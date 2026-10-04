import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .anki import AnkiConnectClient
from .costs import format_polza_cost
from .images import ImageService
from .logging_config import is_full_debug_enabled, log_event
from .models import CardDraft, DraftStatus, ImageSource, MeaningOption
from .polza import PolzaClient
from .profiles import LanguageProfile, UserProfile
from .storage import DraftRepository


logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 3900


class BotDependencies:
    def __init__(
        self,
        drafts: DraftRepository,
        polza: PolzaClient,
        images: ImageService,
        anki: AnkiConnectClient,
        allowed_user_ids: set[int],
        ffmpeg_bin: str = "ffmpeg",
        voice_max_bytes: int = 25_000_000,
        user_profiles: dict[int, UserProfile] | None = None,
        polza_by_language: dict[str, PolzaClient] | None = None,
        images_by_language: dict[str, ImageService] | None = None,
        anki_by_language: dict[str, AnkiConnectClient] | None = None,
        anki_by_user: dict[tuple[int, str], AnkiConnectClient] | None = None,
    ) -> None:
        self.drafts = drafts
        self.polza = polza
        self.images = images
        self.anki = anki
        self.allowed_user_ids = allowed_user_ids
        self.ffmpeg_bin = ffmpeg_bin
        self.voice_max_bytes = voice_max_bytes
        self.user_profiles = user_profiles or {}
        self.polza_by_language = polza_by_language or {}
        self.images_by_language = images_by_language or {}
        self.anki_by_language = anki_by_language or {}
        self.anki_by_user = anki_by_user or {}

    def profile_for(self, user_id: int | None) -> UserProfile:
        if user_id is not None and user_id in self.user_profiles:
            return self.user_profiles[user_id]
        return UserProfile(user_id=user_id or 0, languages={"de": LanguageProfile("de", self.anki.deck_name)})

    def language_for(self, user_id: int | None, language: str | None = None) -> LanguageProfile:
        profile = self.profile_for(user_id)
        selected = language or (self.drafts.get_user_language(user_id) if user_id is not None else profile.default_language)
        return profile.get(selected)

    def polza_for(self, user_id: int | None) -> PolzaClient:
        return self.polza_by_language.get(self.language_for(user_id).language, self.polza)

    def images_for(self, user_id: int | None) -> ImageService:
        return self.images_by_language.get(self.language_for(user_id).language, self.images)

    def anki_for(self, user_id: int | None) -> AnkiConnectClient:
        profile = self.profile_for(user_id)
        selected = self.language_for(user_id)
        if user_id is not None and (user_id, selected.language) in self.anki_by_user:
            return self.anki_by_user[(user_id, selected.language)]
        return self.anki_by_language.get(selected.language, self.anki)


def build_application(token: str, deps: BotDependencies) -> Application:
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(20.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .media_write_timeout(60.0)
        .pool_timeout(20.0)
        .post_init(prepare_application)
        .post_shutdown(close_dependencies)
        .build()
    )
    app.bot_data["deps"] = deps
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def close_dependencies(app: Application) -> None:
    deps = app.bot_data.get("deps")
    if not deps:
        return
    log_event(logger, logging.INFO, "bot.dependencies.close.start", "Closing HTTP clients")
    dependencies = [("polza", deps.polza), ("images", deps.images), ("anki", deps.anki)]
    dependencies.extend((f"polza.{key}", value) for key, value in deps.polza_by_language.items() if value is not deps.polza)
    dependencies.extend((f"images.{key}", value) for key, value in deps.images_by_language.items() if value is not deps.images)
    dependencies.extend((f"anki.{key}", value) for key, value in deps.anki_by_language.items() if value is not deps.anki)
    dependencies.extend((f"anki.user.{key}", value) for key, value in deps.anki_by_user.items() if value is not deps.anki)
    seen: set[int] = set()
    for name, dependency in dependencies:
        if id(dependency) in seen:
            continue
        seen.add(id(dependency))
        try:
            await dependency.close()
            log_event(logger, logging.INFO, "bot.dependencies.close.success", dependency=name)
        except Exception:
            log_event(logger, logging.ERROR, "bot.dependencies.close.error", dependency=name, exc_info=True)


async def prepare_anki(app: Application) -> None:
    deps = app.bot_data.get("deps")
    if not deps:
        return
    clients = [deps.anki, *deps.anki_by_language.values(), *deps.anki_by_user.values()]
    seen: set[int] = set()
    for client in clients:
        if id(client) in seen:
            continue
        seen.add(id(client))
        await client.ensure_deck()
        await client.ensure_note_type()
        await client.ensure_legacy_n8n_tts()
    log_event(logger, logging.INFO, "bot.anki.prepare.success", client_count=len(seen))


async def prepare_application(app: Application) -> None:
    await prepare_anki(app)
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("language", "Выбрать язык карточек"),
        BotCommand("pending", "Показать последний черновик"),
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_event(logger, logging.INFO, "bot.command.start", user_id=_update_user_id(update))
    if not await ensure_authorized(update, context):
        return
    deps = _deps(context)
    user_id = _update_user_id(update)
    profile = deps.profile_for(user_id)
    active = deps.language_for(user_id)
    reply_markup = None
    if len(profile.languages) > 1:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Выбрать язык", callback_data="language_menu:0")]
        ])
    await update.effective_message.reply_text(
        f"Текущий язык: {active.language_name}. Доступны: {', '.join(item.language_name for item in profile.languages.values())}.\n"
        "Отправь русское слово/фразу или фото. Я предложу значения, а после выбора подготовлю Anki-карточку с картинкой.\n"
        "Сменить язык: /language",
        reply_markup=reply_markup,
    )
    log_event(logger, logging.INFO, "bot.command.start.success", user_id=_update_user_id(update))


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update, context):
        return
    user_id = _update_user_id(update)
    deps = _deps(context)
    active = deps.language_for(user_id)
    buttons = [
        [InlineKeyboardButton(
            f"{'✅ ' if language == active.language else ''}{profile.language_name}",
            callback_data=f"language:{language}",
        )]
        for language, profile in deps.profile_for(user_id).languages.items()
    ]
    await update.effective_message.reply_text(
        f"Выбери язык карточек (сейчас: {active.language_name}):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _update_user_id(update)
    log_event(logger, logging.INFO, "bot.command.pending", user_id=user_id)
    if not await ensure_authorized(update, context):
        return
    deps = _deps(context)
    draft = deps.drafts.latest_for_user(user_id)
    if not draft:
        await update.effective_message.reply_text("Черновиков пока нет.")
        log_event(logger, logging.INFO, "bot.command.pending.empty", user_id=user_id)
        return
    log_event(logger, logging.INFO, "bot.command.pending.found", user_id=user_id, draft_id=draft.id)
    await send_preview(update, context, draft)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started_at = time.perf_counter()
    user_id = _update_user_id(update)
    raw_text = update.effective_message.text if update.effective_message and update.effective_message.text else ""
    log_event(
        logger,
        logging.INFO,
        "bot.message.text.received",
        user_id=user_id,
        message_length=len(raw_text),
        source_text=raw_text if is_full_debug_enabled() else None,
    )
    if not await ensure_authorized(update, context):
        return
    deps = _deps(context)
    text = raw_text.strip()
    editing_draft_id = context.user_data.pop("editing_draft_id", None)

    if editing_draft_id:
        log_event(logger, logging.INFO, "bot.manual_edit.start", user_id=user_id, draft_id=editing_draft_id, text=text)
        draft = deps.drafts.get(int(editing_draft_id))
        if not draft:
            await update.effective_message.reply_text("Не нашел черновик для исправления.")
            log_event(logger, logging.WARNING, "bot.manual_edit.missing_draft", user_id=user_id, draft_id=editing_draft_id)
            return
        try:
            updated = await _polza(deps, user_id, draft.language).edit_card(draft, text)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            await update.effective_message.reply_text(f"Не получилось применить исправление: {format_error(exc)}")
            log_event(logger, logging.ERROR, "bot.manual_edit.error", user_id=user_id, draft_id=editing_draft_id, exc_info=True)
            return
        updated = updated.model_copy(update={"image_path": draft.image_path, "image_source": draft.image_source, "audio_path": draft.audio_path})
        updated = deps.drafts.save(updated)
        await update.effective_message.reply_text("Исправление применено через ИИ.")
        log_event(
            logger,
            logging.INFO,
            "bot.manual_edit.success",
            user_id=user_id,
            draft_id=updated.id,
            duration_ms=_duration_ms(started_at),
            draft=updated if is_full_debug_enabled() else None,
        )
        await send_preview(update, context, updated)
        return

    status_message = await update.effective_message.reply_text("Ищу возможные значения...")
    try:
        selection = await _polza(deps, user_id).suggest_meaning_selection(text)
        request_id = store_pending_meaning_request(
            context,
            source_type="text",
            language=_language(deps, user_id),
            source_text=selection.source_text_for_card,
            meanings=selection.options,
            polza_cost_rub=selection.polza_cost_rub,
            polza_cost_complete=selection.polza_cost_complete,
            polza_cost_available=selection.polza_cost_available,
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        await status_message.edit_text(f"Не получилось определить значения: {format_error(exc)}")
        log_event(
            logger,
            logging.ERROR,
            "bot.meaning_selection.error",
            user_id=user_id,
            source_text=text,
            duration_ms=_duration_ms(started_at),
            exc_info=True,
        )
        return

    await status_message.delete()
    await send_meaning_options(update, context, request_id, selection.options)
    log_event(
        logger,
        logging.INFO,
        "bot.meaning_selection.sent",
        user_id=user_id,
        request_id=request_id,
        source_text_for_card=selection.source_text_for_card,
        option_count=len(selection.options),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started_at = time.perf_counter()
    user_id = _update_user_id(update)
    message = update.effective_message
    photos = list(message.photo or []) if message else []
    caption = message.caption.strip() if message and message.caption else ""
    selected_photo = max(photos, key=lambda photo: photo.file_size or photo.width * photo.height) if photos else None
    log_event(
        logger,
        logging.INFO,
        "bot.message.photo.received",
        user_id=user_id,
        photo_count=len(photos),
        caption=caption if is_full_debug_enabled() else None,
        selected_file_id=selected_photo.file_id if selected_photo else None,
        selected_file_unique_id=selected_photo.file_unique_id if selected_photo else None,
        selected_file_size=selected_photo.file_size if selected_photo else None,
    )
    if not await ensure_authorized(update, context):
        return
    if not selected_photo:
        await message.reply_text("Не увидел фото в сообщении.")
        log_event(logger, logging.WARNING, "bot.message.photo.empty", user_id=user_id)
        return

    deps = _deps(context)
    status_message = await message.reply_text("Скачиваю фото и ищу возможные значения...")
    try:
        image_path = await download_telegram_photo(selected_photo, _images(deps, user_id).media_dir, user_id)
        log_event(
            logger,
            logging.INFO,
            "bot.photo_card_generation.start",
            user_id=user_id,
            image_path=image_path,
            caption=caption if is_full_debug_enabled() else None,
        )
        selection = await _polza(deps, user_id).suggest_meaning_selection_from_image(image_path, caption)
        meanings = selection.options
        request_id = store_pending_meaning_request(
            context,
            source_type="photo",
            language=_language(deps, user_id),
            source_text=f"Фото: {caption}".strip() if caption else "Фото",
            meanings=meanings,
            image_path=image_path,
            caption=caption,
            polza_cost_rub=selection.polza_cost_rub,
            polza_cost_complete=selection.polza_cost_complete,
            polza_cost_available=selection.polza_cost_available,
        )
    except (httpx.HTTPError, TelegramError, RuntimeError, ValueError, KeyError) as exc:
        await status_message.edit_text(f"Не получилось подготовить карточку по фото: {format_error(exc)}")
        log_event(
            logger,
            logging.ERROR,
            "bot.photo_meaning_selection.error",
            user_id=user_id,
            duration_ms=_duration_ms(started_at),
            exc_info=True,
        )
        return

    await status_message.delete()
    await send_meaning_options(update, context, request_id, meanings)
    log_event(logger, logging.INFO, "bot.photo_meaning_selection.sent", user_id=user_id, request_id=request_id, option_count=len(meanings), duration_ms=_duration_ms(started_at))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started_at = time.perf_counter()
    user_id = _update_user_id(update)
    message = update.effective_message
    voice = message.voice if message else None
    log_event(
        logger,
        logging.INFO,
        "bot.message.voice.received",
        user_id=user_id,
        file_id=voice.file_id if voice else None,
        file_unique_id=voice.file_unique_id if voice else None,
        file_size=voice.file_size if voice else None,
        duration=voice.duration if voice else None,
    )
    if not await ensure_authorized(update, context):
        return
    if not voice:
        await message.reply_text("Не увидел голосовое сообщение.")
        log_event(logger, logging.WARNING, "bot.message.voice.empty", user_id=user_id)
        return

    deps = _deps(context)
    if voice.file_size and voice.file_size > deps.voice_max_bytes:
        await message.reply_text(f"Голосовое слишком большое: максимум {deps.voice_max_bytes // 1_000_000} MB.")
        log_event(logger, logging.WARNING, "bot.message.voice.too_large", user_id=user_id, file_size=voice.file_size, max_bytes=deps.voice_max_bytes)
        return

    status_message = await message.reply_text("Скачиваю голосовое и распознаю текст...")
    try:
        raw_audio_path = await download_telegram_voice(voice, _images(deps, user_id).media_dir, user_id)
        if raw_audio_path.stat().st_size > deps.voice_max_bytes:
            raise ValueError(f"Голосовое слишком большое: максимум {deps.voice_max_bytes // 1_000_000} MB.")
        audio_path = await convert_audio_to_mp3(raw_audio_path, deps.ffmpeg_bin)
        transcript = await _polza(deps, user_id).transcribe_audio(raw_audio_path, user_id=user_id)
        selection = await _polza(deps, user_id).suggest_meaning_selection_from_audio(transcript)
        request_id = store_pending_meaning_request(
            context,
            source_type="voice",
            language=_language(deps, user_id),
            source_text=selection.source_text_for_card,
            meanings=selection.options,
            audio_path=audio_path,
            primary_full_phrase=True,
            polza_cost_rub=selection.polza_cost_rub,
            polza_cost_complete=selection.polza_cost_complete,
            polza_cost_available=selection.polza_cost_available,
        )
    except (httpx.HTTPError, TelegramError, ValueError, KeyError, RuntimeError) as exc:
        await status_message.edit_text(f"Не получилось обработать голосовое: {format_error(exc)}")
        log_event(
            logger,
            logging.ERROR,
            "bot.voice_meaning_selection.error",
            user_id=user_id,
            duration_ms=_duration_ms(started_at),
            exc_info=True,
        )
        return

    await message.reply_text(f"Распознал: {transcript}")
    await status_message.delete()
    await send_meaning_options(update, context, request_id, selection.options)
    log_event(
        logger,
        logging.INFO,
        "bot.voice_meaning_selection.sent",
        user_id=user_id,
        request_id=request_id,
        source_text_for_card=selection.source_text_for_card,
        audio_path=audio_path,
        option_count=len(selection.options),
        duration_ms=_duration_ms(started_at),
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started_at = time.perf_counter()
    user_id = _update_user_id(update)
    message = update.effective_message
    audio = message.audio if message and message.audio else message.document if message else None
    log_event(
        logger,
        logging.INFO,
        "bot.message.audio.received",
        user_id=user_id,
        file_id=audio.file_id if audio else None,
        file_unique_id=audio.file_unique_id if audio else None,
        file_size=audio.file_size if audio else None,
        file_name=getattr(audio, "file_name", None),
        mime_type=getattr(audio, "mime_type", None),
        duration=getattr(audio, "duration", None),
    )
    if not await ensure_authorized(update, context):
        return
    if not audio:
        await message.reply_text("Не увидел аудиофайл.")
        log_event(logger, logging.WARNING, "bot.message.audio.empty", user_id=user_id)
        return

    deps = _deps(context)
    if audio.file_size and audio.file_size > deps.voice_max_bytes:
        await message.reply_text(f"Аудиофайл слишком большой: максимум {deps.voice_max_bytes // 1_000_000} MB.")
        log_event(logger, logging.WARNING, "bot.message.audio.too_large", user_id=user_id, file_size=audio.file_size, max_bytes=deps.voice_max_bytes)
        return

    status_message = await message.reply_text("Скачиваю аудио и распознаю текст...")
    try:
        raw_audio_path = await download_telegram_audio(audio, _images(deps, user_id).media_dir, user_id)
        if raw_audio_path.stat().st_size > deps.voice_max_bytes:
            raise ValueError(f"Аудиофайл слишком большой: максимум {deps.voice_max_bytes // 1_000_000} MB.")
        audio_path = await convert_audio_to_mp3(raw_audio_path, deps.ffmpeg_bin)
        transcript = await _polza(deps, user_id).transcribe_audio(raw_audio_path, user_id=user_id)
        selection = await _polza(deps, user_id).suggest_meaning_selection_from_audio(transcript)
        request_id = store_pending_meaning_request(
            context,
            source_type="audio",
            language=_language(deps, user_id),
            source_text=selection.source_text_for_card,
            meanings=selection.options,
            audio_path=audio_path,
            primary_full_phrase=True,
            polza_cost_rub=selection.polza_cost_rub,
            polza_cost_complete=selection.polza_cost_complete,
            polza_cost_available=selection.polza_cost_available,
        )
    except (httpx.HTTPError, TelegramError, ValueError, KeyError, RuntimeError) as exc:
        await status_message.edit_text(f"Не получилось обработать аудио: {format_error(exc)}")
        log_event(
            logger,
            logging.ERROR,
            "bot.audio_meaning_selection.error",
            user_id=user_id,
            duration_ms=_duration_ms(started_at),
            exc_info=True,
        )
        return

    await message.reply_text(f"Распознал: {transcript}")
    await status_message.delete()
    await send_meaning_options(update, context, request_id, selection.options)
    log_event(
        logger,
        logging.INFO,
        "bot.audio_meaning_selection.sent",
        user_id=user_id,
        request_id=request_id,
        source_text_for_card=selection.source_text_for_card,
        audio_path=audio_path,
        option_count=len(selection.options),
        duration_ms=_duration_ms(started_at),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started_at = time.perf_counter()
    user_id = _update_user_id(update)
    raw_data = update.callback_query.data if update.callback_query else ""
    log_event(logger, logging.INFO, "bot.callback.received", user_id=user_id, callback_data=raw_data)
    if not await ensure_authorized(update, context):
        return
    deps = _deps(context)
    query = update.callback_query
    await query.answer()
    action, draft_id_raw = query.data.split(":", 1)
    if action == "language_menu":
        await language_command(update, context)
        return
    if action == "language":
        await handle_language_callback(update, context, draft_id_raw)
        return
    if action == "meaning":
        await handle_meaning_callback(update, context, draft_id_raw, started_at)
        return
    if action == "meaning_cancel":
        await handle_meaning_cancel_callback(update, context, draft_id_raw)
        return

    draft = deps.drafts.get(int(draft_id_raw))
    if not draft:
        await query.edit_message_text(text="Черновик не найден.")
        log_event(logger, logging.WARNING, "bot.callback.missing_draft", user_id=user_id, action=action, draft_id=draft_id_raw)
        return

    if action == "add":
        log_event(logger, logging.INFO, "bot.callback.add.start", user_id=user_id, draft_id=draft.id)
        deps.drafts.update_status(draft.id, DraftStatus.APPROVED)
        try:
            anki_note_id = await _anki(deps, user_id, draft.language).add_card(draft)
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            await query.message.reply_text(f"Не получилось добавить карточку в Anki: {format_error(exc)}")
            log_event(
                logger,
                logging.ERROR,
                "bot.callback.add.error",
                user_id=user_id,
                draft_id=draft.id,
                duration_ms=_duration_ms(started_at),
                exc_info=True,
            )
            return
        deps.drafts.update_status(draft.id, DraftStatus.SENT)
        cost_text = format_polza_cost(
            draft.polza_cost_rub,
            complete=draft.polza_cost_complete,
            available=draft.polza_cost_available,
        )
        await query.edit_message_text(
            text=f"Добавлено в Anki. Note ID: {anki_note_id}. {cost_text}"
        )
        log_event(
            logger,
            logging.INFO,
            "bot.callback.add.success",
            user_id=user_id,
            draft_id=draft.id,
            anki_note_id=anki_note_id,
            duration_ms=_duration_ms(started_at),
        )
        return

    if action == "cancel":
        deps.drafts.update_status(draft.id, DraftStatus.REJECTED)
        await query.edit_message_text(text="Черновик отменен.")
        log_event(logger, logging.INFO, "bot.callback.cancel.success", user_id=user_id, draft_id=draft.id)
        return

    if action == "edit":
        context.user_data["editing_draft_id"] = draft.id
        await query.message.reply_text(
            "Напиши, что исправить в карточке. Например: «замени пример на рабочую ситуацию» или «перевод должен быть про еду, а не про глагол»."
        )
        log_event(logger, logging.INFO, "bot.callback.edit.awaiting_text", user_id=user_id, draft_id=draft.id)
        return

    if action == "image":
        log_event(logger, logging.INFO, "bot.callback.image.start", user_id=user_id, draft_id=draft.id)
        try:
            updated = await _images(deps, user_id, draft.language).attach_image(draft)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            await query.message.reply_text(f"Не получилось обновить картинку: {format_error(exc)}")
            log_event(logger, logging.ERROR, "bot.callback.image.error", user_id=user_id, draft_id=draft.id, exc_info=True)
            return
        updated = deps.drafts.save(updated)
        await query.message.reply_text("Обновил картинку.")
        log_event(logger, logging.INFO, "bot.callback.image.success", user_id=user_id, draft_id=updated.id, duration_ms=_duration_ms(started_at))
        await send_preview(update, context, updated)
        return

    log_event(logger, logging.WARNING, "bot.callback.unknown_action", user_id=user_id, action=action, draft_id=draft.id)


async def handle_language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, language: str) -> None:
    user_id = _update_user_id(update)
    deps = _deps(context)
    profile = deps.profile_for(user_id)
    if language not in profile.languages:
        await update.callback_query.edit_message_text("Этот язык недоступен для твоего профиля.")
        return
    deps.drafts.set_user_language(user_id, language)
    selected = profile.languages[language]
    await update.callback_query.edit_message_text(f"Выбран язык: {selected.language_name}. Колода: {selected.deck_name}")
    log_event(logger, logging.INFO, "bot.language.changed", user_id=user_id, language=language, deck_name=selected.deck_name)


async def handle_meaning_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    payload: str,
    started_at: float,
) -> None:
    user_id = _update_user_id(update)
    deps = _deps(context)
    query = update.callback_query
    try:
        request_id, index_raw = payload.split(":", 1)
        index = int(index_raw)
        pending = context.user_data.get("pending_meanings", {})[request_id]
        language = str(pending.get("language") or _language(deps, user_id))
        meaning = MeaningOption.model_validate(pending["meanings"][index])
    except (KeyError, IndexError, ValueError):
        await query.message.reply_text("Не нашел выбранное значение. Отправь слово или фото заново.")
        log_event(logger, logging.WARNING, "bot.meaning_callback.missing", user_id=user_id, payload=payload)
        return

    status_message = await query.message.reply_text("Готовлю карточку для выбранного значения...")
    try:
        source_type = pending["source_type"]
        source_text = pending["source_text"]
        cost_kwargs = {
            "polza_cost_rub": Decimal(str(pending.get("polza_cost_rub", "0"))),
            "polza_cost_complete": bool(pending.get("polza_cost_complete", True)),
            "polza_cost_available": bool(pending.get("polza_cost_available", False)),
        }
        if source_type == "photo":
            image_path = Path(pending["image_path"])
            draft = await _polza(deps, user_id, language).generate_card_for_meaning(
                user_id=user_id,
                source_text=source_text,
                meaning=meaning,
                image_path=image_path,
                caption=pending.get("caption", ""),
                **cost_kwargs,
            )
            draft = draft.model_copy(update={"language": language, "image_path": image_path, "image_source": ImageSource.TELEGRAM_PHOTO})
            draft = deps.drafts.save(draft)
        elif source_type in {"voice", "audio"}:
            audio_path = Path(pending["audio_path"])
            draft = await _polza(deps, user_id, language).generate_card_for_meaning(
                user_id=user_id,
                source_text=source_text,
                meaning=meaning,
                **cost_kwargs,
            )
            draft = draft.model_copy(update={"language": language, "audio_path": audio_path})
            draft = deps.drafts.save(draft)
            draft = await _images(deps, user_id, language).attach_image(draft)
            draft = deps.drafts.save(draft)
        else:
            draft = await _polza(deps, user_id, language).generate_card_for_meaning(
                user_id=user_id,
                source_text=source_text,
                meaning=meaning,
                **cost_kwargs,
            )
            draft = deps.drafts.save(draft.model_copy(update={"language": language}))
            draft = await _images(deps, user_id, language).attach_image(draft)
            draft = deps.drafts.save(draft)
    except (httpx.HTTPError, TelegramError, RuntimeError, ValueError, KeyError) as exc:
        await status_message.edit_text(f"Не получилось подготовить карточку: {format_error(exc)}")
        log_event(logger, logging.ERROR, "bot.meaning_callback.error", user_id=user_id, payload=payload, exc_info=True)
        return

    await status_message.delete()
    await query.message.delete()
    context.user_data.get("pending_meanings", {}).pop(request_id, None)
    log_event(
        logger,
        logging.INFO,
        "bot.meaning_callback.success",
        user_id=user_id,
        draft_id=draft.id,
        meaning=meaning if is_full_debug_enabled() else meaning.short_label,
        duration_ms=_duration_ms(started_at),
        draft=draft if is_full_debug_enabled() else None,
    )
    await send_preview(update, context, draft)


async def handle_meaning_cancel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request_id: str,
) -> None:
    user_id = _update_user_id(update)
    removed = context.user_data.get("pending_meanings", {}).pop(request_id, None)
    await update.callback_query.edit_message_text(text="Выбор значения отменен.")
    log_event(
        logger,
        logging.INFO,
        "bot.meaning_cancel.success",
        user_id=user_id,
        request_id=request_id,
        found=bool(removed),
    )


async def send_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: CardDraft) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Добавить", callback_data=f"add:{draft.id}")],
            [InlineKeyboardButton("Исправить", callback_data=f"edit:{draft.id}")],
            [InlineKeyboardButton("Другая картинка", callback_data=f"image:{draft.id}")],
            [InlineKeyboardButton("Отмена", callback_data=f"cancel:{draft.id}")],
        ]
    )
    message = update.effective_message
    preview = draft.preview_text()
    chunks = split_telegram_text(preview)
    has_image = bool(draft.image_path and Path(draft.image_path).exists())
    log_event(
        logger,
        logging.INFO,
        "bot.preview.send.start",
        user_id=_update_user_id(update),
        draft_id=draft.id,
        has_image=has_image,
        image_path=draft.image_path,
        caption=preview if is_full_debug_enabled() else None,
        chunk_count=len(chunks),
    )
    if has_image:
        with Path(draft.image_path).open("rb") as image_file:
            try:
                await message.reply_photo(photo=image_file)
                log_event(logger, logging.INFO, "bot.preview.send.photo_success", draft_id=draft.id, image_path=draft.image_path)
            except TimedOut:
                await message.reply_text(
                    "Картинка найдена, но Telegram не успел ее отправить. Попробуй «Другая картинка» или повтори /pending."
                )
                log_event(logger, logging.WARNING, "bot.preview.send.photo_timeout_fallback", draft_id=draft.id, image_path=draft.image_path)
    for index, chunk in enumerate(chunks):
        reply_markup = keyboard if index == len(chunks) - 1 else None
        await message.reply_text(chunk, reply_markup=reply_markup)
    log_event(logger, logging.INFO, "bot.preview.send.text_success", draft_id=draft.id, chunk_count=len(chunks))


async def send_meaning_options(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request_id: str,
    meanings: list[MeaningOption],
) -> None:
    rows = []
    lines = ["Выбери конкретное значение для карточки:"]
    for index, meaning in enumerate(meanings):
        label = meaning.short_label or meaning.word or meaning.translation_ru or f"Вариант {index + 1}"
        target = meaning_target_text(meaning)
        button_label = meaning_button_label(index, target or label)
        rows.append([InlineKeyboardButton(button_label, callback_data=f"meaning:{request_id}:{index}")])
        lines.append(meaning_option_text(index, meaning))
    rows.append([InlineKeyboardButton("Отмена", callback_data=f"meaning_cancel:{request_id}")])
    await update.effective_message.reply_text("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


ARTICLE_PREFIXES = ("der", "die", "das", "ein", "eine", "einen", "einem", "einer", "eines", "den", "dem", "des")


def meaning_target_text(meaning: MeaningOption) -> str:
    word = meaning.word.strip()
    article = meaning.article.strip()
    if not word:
        return meaning.translation_ru.strip() or meaning.short_label.strip()
    first_word = word.split(maxsplit=1)[0].casefold()
    if article and first_word not in ARTICLE_PREFIXES:
        return f"{article} {word}"
    return word


def meaning_option_text(index: int, meaning: MeaningOption) -> str:
    label = meaning.short_label or meaning.word or meaning.translation_ru or f"Вариант {index + 1}"
    target = meaning_target_text(meaning)
    details = meaning.explanation_ru or meaning.meaning_ru
    marker = " [полная фраза из аудио]" if index == 0 and "полная фраза из аудио" in details.casefold() else ""
    text = f"{index + 1}. {label}{marker}"
    if target and target != label:
        text += f"\nКарточка: {target}"
    return text + (f"\n{details}" if details else "")


def meaning_button_label(index: int, label: str, limit: int = 60) -> str:
    text = f"{index + 1}. {label}"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def store_pending_meaning_request(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source_type: str,
    language: str = "de",
    source_text: str,
    meanings: list[MeaningOption],
    image_path: Path | None = None,
    audio_path: Path | None = None,
    caption: str = "",
    primary_full_phrase: bool = False,
    polza_cost_rub: Decimal = Decimal("0"),
    polza_cost_complete: bool = True,
    polza_cost_available: bool = False,
) -> str:
    request_id = uuid4().hex
    pending = context.user_data.setdefault("pending_meanings", {})
    pending[request_id] = {
        "source_type": source_type,
        "language": language,
        "source_text": source_text,
        "meanings": [meaning.model_dump(mode="json") for meaning in meanings],
        "image_path": str(image_path) if image_path else None,
        "audio_path": str(audio_path) if audio_path else None,
        "caption": caption,
        "primary_full_phrase": primary_full_phrase,
        "polza_cost_rub": str(polza_cost_rub),
        "polza_cost_complete": polza_cost_complete,
        "polza_cost_available": polza_cost_available,
    }
    return request_id


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


def apply_manual_edit(draft: CardDraft, text: str) -> CardDraft:
    if ":" not in text:
        log_event(logger, logging.INFO, "bot.manual_edit.apply", draft_id=draft.id, field="grammar_note_ru", value=text)
        return draft.model_copy(update={"grammar_note_ru": text})
    field, value = [part.strip() for part in text.split(":", 1)]
    aliases = {
        "german": "word",
        "de": "word",
        "word": "word",
        "article": "article",
        "gender": "gender_color",
        "gender_color": "gender_color",
        "level": "level",
        "russian": "translation_ru",
        "ru": "translation_ru",
        "translation": "translation_ru",
        "translation_ru": "translation_ru",
        "sentence_de": "sentence_de",
        "sentence_ru": "sentence_ru",
        "example_de": "sentence_de",
        "example_ru": "sentence_ru",
        "meaning": "meaning_ru",
        "meaning_ru": "meaning_ru",
        "other_meanings": "other_meanings_ru",
        "other_meanings_ru": "other_meanings_ru",
        "definition": "definition_ru",
        "definition_ru": "definition_ru",
        "synonyms": "synonyms",
        "grammar": "grammar_table",
        "grammar_table": "grammar_table",
        "etymology": "etymology",
        "style": "style_and_group",
        "style_and_group": "style_and_group",
        "image_query": "image_query",
    }
    target = aliases.get(field.lower())
    if not target:
        log_event(logger, logging.INFO, "bot.manual_edit.apply_unknown_field", draft_id=draft.id, field=field, value=value)
        return draft.model_copy(update={"grammar_note_ru": text})

    update = {target: value}
    if target == "word":
        update["word_or_phrase_de"] = value
    if target == "sentence_de":
        update["example_de"] = value
    if target == "sentence_ru":
        update["example_ru"] = value
    if target == "grammar_table":
        update["grammar_note_ru"] = value
    log_event(logger, logging.INFO, "bot.manual_edit.apply", draft_id=draft.id, field=field, target=target, value=value, update=update)
    return draft.model_copy(update=update)


async def download_telegram_photo(photo, media_dir: Path, user_id: int | None) -> Path:
    started_at = time.perf_counter()
    media_dir.mkdir(parents=True, exist_ok=True)
    telegram_file = await photo.get_file()
    suffix = Path(telegram_file.file_path or "").suffix.lower() or ".jpg"
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    path = media_dir / f"telegram_{user_id or 'unknown'}_{uuid4().hex}{suffix}"
    log_event(
        logger,
        logging.INFO,
        "bot.photo.download.start",
        user_id=user_id,
        file_id=photo.file_id,
        file_unique_id=photo.file_unique_id,
        file_size=photo.file_size,
        path=path,
    )
    await telegram_file.download_to_drive(custom_path=path)
    log_event(
        logger,
        logging.INFO,
        "bot.photo.download.success",
        user_id=user_id,
        file_id=photo.file_id,
        path=path,
        size_bytes=path.stat().st_size,
        duration_ms=_duration_ms(started_at),
    )
    return path


async def download_telegram_voice(voice, media_dir: Path, user_id: int | None) -> Path:
    started_at = time.perf_counter()
    media_dir.mkdir(parents=True, exist_ok=True)
    telegram_file = await voice.get_file()
    suffix = Path(telegram_file.file_path or "").suffix.lower() or ".ogg"
    if suffix not in {".oga", ".ogg", ".opus", ".webm", ".m4a", ".mp3"}:
        suffix = ".ogg"
    path = media_dir / f"telegram_voice_{user_id or 'unknown'}_{uuid4().hex}{suffix}"
    log_event(
        logger,
        logging.INFO,
        "bot.voice.download.start",
        user_id=user_id,
        file_id=voice.file_id,
        file_unique_id=voice.file_unique_id,
        file_size=voice.file_size,
        path=path,
    )
    await telegram_file.download_to_drive(custom_path=path)
    log_event(
        logger,
        logging.INFO,
        "bot.voice.download.success",
        user_id=user_id,
        file_id=voice.file_id,
        path=path,
        size_bytes=path.stat().st_size,
        duration_ms=_duration_ms(started_at),
    )
    return path


async def download_telegram_audio(audio, media_dir: Path, user_id: int | None) -> Path:
    started_at = time.perf_counter()
    media_dir.mkdir(parents=True, exist_ok=True)
    telegram_file = await audio.get_file()
    source_name = getattr(audio, "file_name", "") or telegram_file.file_path or ""
    suffix = Path(source_name).suffix.lower() or ".mp3"
    if suffix not in {".mp3", ".m4a", ".ogg", ".oga", ".opus", ".webm", ".wav"}:
        suffix = ".mp3"
    path = media_dir / f"telegram_audio_{user_id or 'unknown'}_{uuid4().hex}{suffix}"
    log_event(
        logger,
        logging.INFO,
        "bot.audio.download.start",
        user_id=user_id,
        file_id=audio.file_id,
        file_unique_id=audio.file_unique_id,
        file_size=audio.file_size,
        file_name=getattr(audio, "file_name", None),
        mime_type=getattr(audio, "mime_type", None),
        path=path,
    )
    await telegram_file.download_to_drive(custom_path=path)
    log_event(
        logger,
        logging.INFO,
        "bot.audio.download.success",
        user_id=user_id,
        file_id=audio.file_id,
        path=path,
        size_bytes=path.stat().st_size,
        duration_ms=_duration_ms(started_at),
    )
    return path


async def convert_audio_to_mp3(input_path: Path, ffmpeg_bin: str) -> Path:
    started_at = time.perf_counter()
    if input_path.suffix.lower() == ".mp3":
        log_event(logger, logging.INFO, "bot.audio.convert.skip_mp3", input_path=input_path)
        return input_path
    output_path = input_path.with_suffix(".mp3")
    command = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        str(output_path),
    ]
    log_event(logger, logging.INFO, "bot.voice.convert.start", input_path=input_path, output_path=output_path, ffmpeg_bin=ffmpeg_bin)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        log_event(logger, logging.ERROR, "bot.voice.convert.ffmpeg_missing", ffmpeg_bin=ffmpeg_bin, exc_info=True)
        raise RuntimeError("ffmpeg не найден. Установи ffmpeg или укажи FFMPEG_BIN в .env.") from exc

    stdout, stderr = await process.communicate()
    if process.returncode != 0 or not output_path.exists():
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        log_event(
            logger,
            logging.ERROR,
            "bot.voice.convert.error",
            input_path=input_path,
            output_path=output_path,
            returncode=process.returncode,
            stdout=stdout_text if is_full_debug_enabled() else None,
            stderr=stderr_text if is_full_debug_enabled() else None,
        )
        raise RuntimeError(f"ffmpeg не смог конвертировать голосовое в MP3: {stderr_text or stdout_text or process.returncode}")
    log_event(
        logger,
        logging.INFO,
        "bot.voice.convert.success",
        input_path=input_path,
        output_path=output_path,
        size_bytes=output_path.stat().st_size,
        duration_ms=_duration_ms(started_at),
    )
    return output_path


def is_authorized_user(user_id: int | None, allowed_user_ids: set[int]) -> bool:
    return user_id is not None and (not allowed_user_ids or user_id in allowed_user_ids)


async def ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    deps = _deps(context)
    user_id = update.effective_user.id if update.effective_user else None
    if is_authorized_user(user_id, deps.allowed_user_ids):
        log_event(logger, logging.DEBUG, "bot.auth.allowed", user_id=user_id)
        return True
    log_event(logger, logging.WARNING, "bot.auth.denied", user_id=user_id, has_callback=bool(update.callback_query))
    if update.callback_query:
        await update.callback_query.answer("Нет доступа.", show_alert=False)
    elif update.effective_message:
        await update.effective_message.reply_text("Нет доступа к этому боту.")
    return False


def format_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "сервис недоступен. Проверь AnkiConnect, интернет или настройки API."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}. Проверь ключи, модели и баланс."
    return str(exc)


def _deps(context: ContextTypes.DEFAULT_TYPE) -> BotDependencies:
    return context.application.bot_data["deps"]


def _polza(deps, user_id: int | None, language: str | None = None):
    if hasattr(deps, "polza_by_language") and language:
        return deps.polza_by_language.get(language, deps.polza)
    return deps.polza_for(user_id) if hasattr(deps, "polza_for") else deps.polza


def _language(deps, user_id: int | None) -> str:
    return deps.language_for(user_id).language if hasattr(deps, "language_for") else "de"


def _images(deps, user_id: int | None, language: str | None = None):
    if hasattr(deps, "images_by_language") and language:
        return deps.images_by_language.get(language, deps.images)
    return deps.images_for(user_id) if hasattr(deps, "images_for") else deps.images


def _anki(deps, user_id: int | None, language: str | None = None):
    if hasattr(deps, "anki_by_user") and language and user_id is not None:
        return deps.anki_by_user.get((user_id, language), deps.anki)
    return deps.anki_for(user_id) if hasattr(deps, "anki_for") else deps.anki


def _update_user_id(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def _duration_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)
