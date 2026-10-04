import logging

from .anki import AnkiConnectClient
from .bot import BotDependencies, build_application
from .config import load_settings
from .images import ImageService
from .logging_config import configure_logging, log_event
from .polza import PolzaClient
from .storage import DraftRepository


logger = logging.getLogger(__name__)


def main() -> None:
    settings = load_settings()
    user_profiles = settings.user_profiles
    allowed_user_ids = set(user_profiles)
    configure_logging(
        level=settings.app_log_level,
        log_file=settings.app_log_file,
        max_bytes=settings.app_log_max_bytes,
        backup_count=settings.app_log_backup_count,
        full_debug=settings.app_log_full_debug,
        secrets=(settings.telegram_bot_token, settings.polza_ai_api_key),
    )
    log_event(
        logger,
        logging.INFO,
        "app.starting",
        "Starting Anki Telegram bot",
        settings={
            "allowed_telegram_user_ids": sorted(allowed_user_ids),
            "telegram_user_profiles": {user_id: sorted(profile.languages) for user_id, profile in user_profiles.items()},
            "anki_connect_url": settings.anki_connect_url,
            "anki_deck_name": settings.anki_deck_name,
            "anki_note_type": settings.anki_note_type,
            "polza_base_url": settings.polza_base_url,
            "polza_text_model": settings.polza_text_model,
            "polza_image_model": settings.polza_image_model,
            "polza_transcription_model": settings.polza_transcription_model,
            "app_db_path": settings.app_db_path,
            "app_media_dir": settings.app_media_dir,
            "ffmpeg_bin": settings.ffmpeg_bin,
            "voice_max_bytes": settings.voice_max_bytes,
            "app_log_file": settings.app_log_file,
            "app_log_level": settings.app_log_level,
            "app_log_full_debug": settings.app_log_full_debug,
        },
    )
    try:
        drafts = DraftRepository(settings.app_db_path)
        languages = {language for profile in user_profiles.values() for language in profile.languages} or {"de"}
        polza_by_language = {
            language: PolzaClient(
                api_key=settings.polza_ai_api_key,
                base_url=settings.polza_base_url,
                text_model=settings.polza_text_model,
                image_model=settings.polza_image_model,
                transcription_model=settings.polza_transcription_model,
                language=language,
            )
            for language in languages
        }
        images_by_language = {
            language: ImageService(polza=polza, media_dir=settings.app_media_dir)
            for language, polza in polza_by_language.items()
        }
        anki_by_language = {
            language: AnkiConnectClient(
                url=settings.anki_connect_url,
                deck_name=settings.anki_deck_name,
                note_type={"de": settings.anki_note_type, "en": "English Context v1", "ko": "Korean Context v1"}[language],
                language=language,
            )
            for language in languages
        }
        anki_by_user = {
            (user_id, language): AnkiConnectClient(
                url=settings.anki_connect_url,
                deck_name=language_profile.deck_name,
                note_type=settings.anki_note_type if language == "de" else language_profile.note_type,
                language=language,
            )
            for user_id, profile in user_profiles.items()
            for language, language_profile in profile.languages.items()
        }
        polza = polza_by_language["de"] if "de" in polza_by_language else next(iter(polza_by_language.values()))
        images = images_by_language["de"] if "de" in images_by_language else next(iter(images_by_language.values()))
        anki = anki_by_language["de"] if "de" in anki_by_language else next(iter(anki_by_language.values()))
        app = build_application(
            settings.telegram_bot_token,
            BotDependencies(
                drafts,
                polza,
                images,
                anki,
                allowed_user_ids,
                settings.ffmpeg_bin,
                settings.voice_max_bytes,
                user_profiles,
                polza_by_language,
                images_by_language,
                anki_by_language,
                anki_by_user,
            ),
        )
        log_event(logger, logging.INFO, "app.polling.start", "Starting Telegram polling")
        app.run_polling()
        log_event(logger, logging.INFO, "app.polling.stop", "Telegram polling stopped")
    except Exception:
        log_event(logger, logging.CRITICAL, "app.fatal", "Fatal application error", exc_info=True)
        raise


if __name__ == "__main__":
    main()
