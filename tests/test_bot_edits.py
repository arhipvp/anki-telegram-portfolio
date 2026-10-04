import asyncio
from decimal import Decimal
from pathlib import Path

from anki_telegram_bot.bot import (
    apply_manual_edit,
    build_application,
    handle_callback,
    handle_audio,
    handle_meaning_callback,
    handle_text,
    handle_voice,
    handle_meaning_cancel_callback,
    is_authorized_user,
    meaning_button_label,
    meaning_option_text,
    meaning_target_text,
    split_telegram_text,
    store_pending_meaning_request,
)
from anki_telegram_bot.models import CardDraft, MeaningOption, MeaningSelection, UnitType


def test_apply_manual_edit_updates_translation() -> None:
    draft = CardDraft(
        user_id=1,
        source_text="Versicherung",
        unit_type=UnitType.WORD,
        word_or_phrase_de="die Versicherung",
        translation_ru="страхование",
        example_de="Ich brauche eine Versicherung.",
        example_ru="Мне нужна страховка.",
    )

    updated = apply_manual_edit(draft, "russian: страховка")

    assert updated.translation_ru == "страховка"
    assert updated.word_or_phrase_de == "die Versicherung"


def test_build_application_does_not_register_audio_handlers() -> None:
    deps = type(
        "FakeDeps",
        (),
        {
            "drafts": object(),
            "polza": object(),
            "images": object(),
            "anki": object(),
            "allowed_user_ids": set(),
            "ffmpeg_bin": "ffmpeg",
            "voice_max_bytes": 25_000_000,
        },
    )()

    app = build_application("123:ABC", deps)
    callbacks = [getattr(handler.callback, "__name__", "") for group in app.handlers.values() for handler in group]

    assert "handle_audio" not in callbacks
    assert "handle_voice" not in callbacks


def test_apply_manual_edit_updates_new_sentence_alias() -> None:
    draft = CardDraft(
        user_id=1,
        source_text="Versicherung",
        word_or_phrase_de="die Versicherung",
        translation_ru="страхование",
        example_de="Ich brauche eine Versicherung.",
        example_ru="Мне нужна страховка.",
    )

    updated = apply_manual_edit(draft, "sentence_de: Ich brauche <b>eine Versicherung</b>.")

    assert updated.sentence_de == "Ich brauche <b>eine Versicherung</b>."
    assert updated.example_de == "Ich brauche <b>eine Versicherung</b>."


def test_apply_manual_edit_updates_legacy_german_alias() -> None:
    draft = CardDraft(
        user_id=1,
        source_text="Versicherung",
        word_or_phrase_de="die Versicherung",
        translation_ru="страхование",
        example_de="Ich brauche eine Versicherung.",
        example_ru="Мне нужна страховка.",
    )

    updated = apply_manual_edit(draft, "german: Versicherung")

    assert updated.word == "Versicherung"
    assert updated.word_or_phrase_de == "Versicherung"


def test_authorized_user_is_allowed() -> None:
    assert is_authorized_user(100000001, {100000001}) is True


def test_unknown_user_is_blocked() -> None:
    assert is_authorized_user(1, {100000001}) is False


def test_empty_allowlist_blocks_missing_user() -> None:
    assert is_authorized_user(None, set()) is False


def test_split_telegram_text_keeps_chunks_under_limit() -> None:
    chunks = split_telegram_text("a" * 10 + "\n\n" + "b" * 10, limit=12)

    assert chunks == ["a" * 10, "b" * 10]


def test_store_pending_meaning_request_uses_json_models(tmp_path) -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.user_data = {}

    context = FakeContext()
    request_id = store_pending_meaning_request(
        context,
        source_type="text",
        source_text="Essen",
        meanings=[MeaningOption(word="essen", translation_ru="есть", short_label="essen / есть")],
        polza_cost_rub=Decimal("1.25"),
        polza_cost_complete=False,
        polza_cost_available=True,
    )

    pending = context.user_data["pending_meanings"][request_id]
    assert pending["source_type"] == "text"
    assert pending["source_text"] == "Essen"
    assert pending["meanings"][0]["word"] == "essen"
    assert pending["polza_cost_rub"] == "1.25"
    assert pending["polza_cost_complete"] is False
    assert pending["polza_cost_available"] is True


def test_handle_text_stores_extracted_source_text_for_how_to_say_request() -> None:
    async def run() -> None:
        class FakeStatusMessage:
            def __init__(self) -> None:
                self.deleted = False

            async def edit_text(self, text: str) -> None:
                self.edited_text = text

            async def delete(self) -> None:
                self.deleted = True

        class FakeMessage:
            def __init__(self) -> None:
                self.text = 'Как сказать официанту "без сдачи"'
                self.replies = []
                self.status = FakeStatusMessage()

            async def reply_text(self, text: str, reply_markup=None):
                self.replies.append((text, reply_markup))
                return self.status

        class FakePolza:
            async def suggest_meaning_selection(self, text: str) -> MeaningSelection:
                assert text == 'Как сказать официанту "без сдачи"'
                return MeaningSelection(
                    source_text_for_card="без сдачи",
                    options=[MeaningOption(unit_type=UnitType.PHRASE, word="Stimmt so", translation_ru="без сдачи")],
                )

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {}
                self.application = type(
                    "FakeApplication",
                    (),
                    {
                        "bot_data": {
                            "deps": type(
                                "FakeDeps",
                                (),
                                {
                                    "allowed_user_ids": {100000001},
                                    "polza": FakePolza(),
                                },
                            )()
                        }
                    },
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.effective_message = FakeMessage()

        update = FakeUpdate()
        context = FakeContext()

        await handle_text(update, context)

        pending = next(iter(context.user_data["pending_meanings"].values()))
        assert pending["source_text"] == "без сдачи"
        assert pending["meanings"][0]["word"] == "Stimmt so"
        assert update.effective_message.status.deleted is True

    asyncio.run(run())


def test_handle_voice_transcribes_and_stores_pending_audio(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        raw_path = tmp_path / "voice.ogg"
        raw_path.write_bytes(b"raw")
        mp3_path = tmp_path / "voice.mp3"
        mp3_path.write_bytes(b"mp3")

        async def fake_download(voice, media_dir: Path, user_id: int | None) -> Path:
            return raw_path

        async def fake_convert(path: Path, ffmpeg_bin: str) -> Path:
            assert path == raw_path
            assert ffmpeg_bin == "ffmpeg"
            return mp3_path

        monkeypatch.setattr("anki_telegram_bot.bot.download_telegram_voice", fake_download)
        monkeypatch.setattr("anki_telegram_bot.bot.convert_audio_to_mp3", fake_convert)

        class FakeStatusMessage:
            def __init__(self) -> None:
                self.deleted = False
                self.edited_text = ""

            async def edit_text(self, text: str) -> None:
                self.edited_text = text

            async def delete(self) -> None:
                self.deleted = True

        class FakeMessage:
            def __init__(self) -> None:
                self.voice = type("FakeVoice", (), {"file_id": "file", "file_unique_id": "unique", "file_size": 123, "duration": 3})()
                self.replies = []
                self.status = FakeStatusMessage()

            async def reply_text(self, text: str, reply_markup=None):
                self.replies.append((text, reply_markup))
                return self.status

        class FakePolza:
            async def transcribe_audio(self, path: Path, user_id: int | None = None) -> str:
                assert path == raw_path
                assert user_id == 100000001
                return "оформить страховку"

            async def suggest_meaning_selection_from_audio(self, text: str) -> MeaningSelection:
                assert text == "оформить страховку"
                return MeaningSelection(
                    source_text_for_card="оформить страховку",
                    options=[
                        MeaningOption(
                            unit_type=UnitType.PHRASE,
                            word="eine Versicherung abschließen",
                            translation_ru="оформить страховку",
                            explanation_ru="Полная фраза из аудио.",
                        )
                    ],
                )

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {}
                self.application = type(
                    "FakeApplication",
                    (),
                    {
                        "bot_data": {
                            "deps": type(
                                "FakeDeps",
                                (),
                                {
                                    "allowed_user_ids": {100000001},
                                    "polza": FakePolza(),
                                    "images": type("FakeImages", (), {"media_dir": tmp_path})(),
                                    "ffmpeg_bin": "ffmpeg",
                                    "voice_max_bytes": 25_000_000,
                                },
                            )()
                        }
                    },
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.effective_message = FakeMessage()

        update = FakeUpdate()
        context = FakeContext()

        await handle_voice(update, context)

        pending = next(iter(context.user_data["pending_meanings"].values()))
        assert pending["source_type"] == "voice"
        assert pending["source_text"] == "оформить страховку"
        assert pending["audio_path"] == str(mp3_path)
        assert pending["primary_full_phrase"] is True
        assert update.effective_message.status.deleted is True
        assert any(text == "Распознал: оформить страховку" for text, _ in update.effective_message.replies)

    asyncio.run(run())


def test_handle_audio_mp3_transcribes_and_stores_pending_audio(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        mp3_path = tmp_path / "song_clip.mp3"
        mp3_path.write_bytes(b"mp3")

        async def fake_download(audio, media_dir: Path, user_id: int | None) -> Path:
            return mp3_path

        monkeypatch.setattr("anki_telegram_bot.bot.download_telegram_audio", fake_download)

        class FakeStatusMessage:
            def __init__(self) -> None:
                self.deleted = False

            async def edit_text(self, text: str) -> None:
                self.edited_text = text

            async def delete(self) -> None:
                self.deleted = True

        class FakeMessage:
            def __init__(self) -> None:
                self.audio = type(
                    "FakeAudio",
                    (),
                    {
                        "file_id": "file",
                        "file_unique_id": "unique",
                        "file_size": 123,
                        "file_name": "song_clip.mp3",
                        "mime_type": "audio/mpeg",
                        "duration": 5,
                    },
                )()
                self.document = None
                self.replies = []
                self.status = FakeStatusMessage()

            async def reply_text(self, text: str, reply_markup=None):
                self.replies.append((text, reply_markup))
                return self.status

        class FakePolza:
            async def transcribe_audio(self, path: Path, user_id: int | None = None) -> str:
                assert path == mp3_path
                return "das Modell"

            async def suggest_meaning_selection_from_audio(self, text: str) -> MeaningSelection:
                assert text == "das Modell"
                return MeaningSelection(
                    source_text_for_card="das Modell",
                    options=[
                        MeaningOption(
                            unit_type=UnitType.PHRASE,
                            word="das Modell",
                            translation_ru="модель",
                            explanation_ru="Полная фраза из аудио.",
                        )
                    ],
                )

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {}
                self.application = type(
                    "FakeApplication",
                    (),
                    {
                        "bot_data": {
                            "deps": type(
                                "FakeDeps",
                                (),
                                {
                                    "allowed_user_ids": {100000001},
                                    "polza": FakePolza(),
                                    "images": type("FakeImages", (), {"media_dir": tmp_path})(),
                                    "ffmpeg_bin": "ffmpeg",
                                    "voice_max_bytes": 25_000_000,
                                },
                            )()
                        }
                    },
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.effective_message = FakeMessage()

        update = FakeUpdate()
        context = FakeContext()

        await handle_audio(update, context)

        pending = next(iter(context.user_data["pending_meanings"].values()))
        assert pending["source_type"] == "audio"
        assert pending["source_text"] == "das Modell"
        assert pending["audio_path"] == str(mp3_path)
        assert pending["primary_full_phrase"] is True
        assert update.effective_message.status.deleted is True
        assert any(text == "Распознал: das Modell" for text, _ in update.effective_message.replies)

    asyncio.run(run())


def test_handle_voice_reports_transcription_failure(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        raw_path = tmp_path / "voice.ogg"
        raw_path.write_bytes(b"raw")
        mp3_path = tmp_path / "voice.mp3"
        mp3_path.write_bytes(b"mp3")

        async def fake_download(voice, media_dir: Path, user_id: int | None) -> Path:
            return raw_path

        async def fake_convert(path: Path, ffmpeg_bin: str) -> Path:
            return mp3_path

        monkeypatch.setattr("anki_telegram_bot.bot.download_telegram_voice", fake_download)
        monkeypatch.setattr("anki_telegram_bot.bot.convert_audio_to_mp3", fake_convert)

        class FakeStatusMessage:
            def __init__(self) -> None:
                self.edited_text = ""

            async def edit_text(self, text: str) -> None:
                self.edited_text = text

            async def delete(self) -> None:
                return None

        class FakeMessage:
            def __init__(self) -> None:
                self.voice = type("FakeVoice", (), {"file_id": "file", "file_unique_id": "unique", "file_size": 123, "duration": 3})()
                self.status = FakeStatusMessage()

            async def reply_text(self, text: str, reply_markup=None):
                return self.status

        class FakePolza:
            async def transcribe_audio(self, path: Path, user_id: int | None = None) -> str:
                raise ValueError("empty audio transcription")

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {}
                self.application = type(
                    "FakeApplication",
                    (),
                    {
                        "bot_data": {
                            "deps": type(
                                "FakeDeps",
                                (),
                                {
                                    "allowed_user_ids": {100000001},
                                    "polza": FakePolza(),
                                    "images": type("FakeImages", (), {"media_dir": tmp_path})(),
                                    "ffmpeg_bin": "ffmpeg",
                                    "voice_max_bytes": 25_000_000,
                                },
                            )()
                        }
                    },
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.effective_message = FakeMessage()

        update = FakeUpdate()
        await handle_voice(update, FakeContext())

        assert "Не получилось обработать голосовое" in update.effective_message.status.edited_text
        assert "empty audio transcription" in update.effective_message.status.edited_text

    asyncio.run(run())


def test_handle_voice_rejects_oversized_file(tmp_path) -> None:
    async def run() -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.voice = type("FakeVoice", (), {"file_id": "file", "file_unique_id": "unique", "file_size": 30, "duration": 3})()
                self.replies = []

            async def reply_text(self, text: str, reply_markup=None):
                self.replies.append((text, reply_markup))

        class FakePolza:
            called = False

            async def transcribe_audio(self, path: Path, user_id: int | None = None) -> str:
                self.called = True
                return "text"

        polza = FakePolza()

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {}
                self.application = type(
                    "FakeApplication",
                    (),
                    {
                        "bot_data": {
                            "deps": type(
                                "FakeDeps",
                                (),
                                {
                                    "allowed_user_ids": {100000001},
                                    "polza": polza,
                                    "images": type("FakeImages", (), {"media_dir": tmp_path})(),
                                    "ffmpeg_bin": "ffmpeg",
                                    "voice_max_bytes": 20,
                                },
                            )()
                        }
                    },
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.effective_message = FakeMessage()

        update = FakeUpdate()
        await handle_voice(update, FakeContext())

        assert "Голосовое слишком большое" in update.effective_message.replies[0][0]
        assert polza.called is False

    asyncio.run(run())


def test_handle_meaning_callback_preserves_voice_audio(tmp_path) -> None:
    async def run() -> None:
        audio_path = tmp_path / "voice.mp3"
        saved_drafts = []

        class FakeStatusMessage:
            def __init__(self) -> None:
                self.deleted = False

            async def edit_text(self, text: str) -> None:
                self.edited_text = text

            async def delete(self) -> None:
                self.deleted = True

        class FakeMessage:
            def __init__(self) -> None:
                self.status = FakeStatusMessage()
                self.replies = []
                self.deleted = False

            async def reply_text(self, text: str, reply_markup=None):
                self.replies.append((text, reply_markup))
                return self.status

            async def reply_photo(self, photo):
                return None

            async def delete(self) -> None:
                self.deleted = True

        class FakeQuery:
            def __init__(self) -> None:
                self.message = FakeMessage()

        class FakePolza:
            async def generate_card_for_meaning(
                self, user_id: int, source_text: str, meaning: MeaningOption, **kwargs
            ) -> CardDraft:
                return CardDraft(
                    user_id=user_id,
                    source_text=source_text,
                    unit_type=UnitType.PHRASE,
                    word="eine Versicherung abschließen",
                    translation_ru="оформить страховку",
                    sentence_de="Ich möchte <b>eine Versicherung abschließen</b>.",
                    sentence_ru="Я хочу <b>оформить страховку</b>.",
                    meaning_ru="заключить договор",
                    definition_ru="Einen Vertrag abschließen.",
                    grammar_table="<ul><li>Verbphrase</li></ul>",
                    level="B1",
                )

        class FakeImages:
            async def attach_image(self, draft: CardDraft) -> CardDraft:
                return draft

        class FakeDrafts:
            def save(self, draft: CardDraft) -> CardDraft:
                saved = draft.model_copy(update={"id": len(saved_drafts) + 1})
                saved_drafts.append(saved)
                return saved

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {
                    "pending_meanings": {
                        "req": {
                            "source_type": "voice",
                            "source_text": "оформить страховку",
                            "audio_path": str(audio_path),
                            "meanings": [MeaningOption(unit_type=UnitType.PHRASE, word="eine Versicherung abschließen").model_dump(mode="json")],
                        }
                    }
                }
                self.application = type(
                    "FakeApplication",
                    (),
                    {"bot_data": {"deps": type("FakeDeps", (), {"polza": FakePolza(), "images": FakeImages(), "drafts": FakeDrafts()})()}},
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.callback_query = FakeQuery()
                self.effective_message = self.callback_query.message

        update = FakeUpdate()
        context = FakeContext()

        await handle_meaning_callback(update, context, "req:0", 0.0)

        assert saved_drafts[-1].audio_path == audio_path
        assert context.user_data["pending_meanings"] == {}
        assert update.callback_query.message.deleted is True

    asyncio.run(run())


def test_handle_meaning_callback_keeps_menu_and_pending_request_after_generation_failure() -> None:
    async def run() -> None:
        class FakeStatusMessage:
            def __init__(self) -> None:
                self.edited_text = ""

            async def edit_text(self, text: str) -> None:
                self.edited_text = text

        class FakeMessage:
            def __init__(self) -> None:
                self.status = FakeStatusMessage()
                self.deleted = False

            async def reply_text(self, text: str, reply_markup=None):
                return self.status

            async def delete(self) -> None:
                self.deleted = True

        class FakeQuery:
            def __init__(self) -> None:
                self.message = FakeMessage()

        class FakePolza:
            async def generate_card_for_meaning(
                self, user_id: int, source_text: str, meaning: MeaningOption, **kwargs
            ) -> CardDraft:
                raise RuntimeError("Polza трижды вернула неполные данные.")

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {
                    "pending_meanings": {
                        "req": {
                            "source_type": "text",
                            "source_text": "Einrichtung",
                            "meanings": [MeaningOption(word="Einrichtung").model_dump(mode="json")],
                        }
                    }
                }
                self.application = type(
                    "FakeApplication",
                    (),
                    {"bot_data": {"deps": type("FakeDeps", (), {"polza": FakePolza()})()}},
                )()

        class FakeUpdate:
            def __init__(self) -> None:
                self.effective_user = type("FakeUser", (), {"id": 100000001})()
                self.callback_query = FakeQuery()
                self.effective_message = self.callback_query.message

        update = FakeUpdate()
        context = FakeContext()

        await handle_meaning_callback(update, context, "req:0", 0.0)

        assert "трижды вернула неполные данные" in update.callback_query.message.status.edited_text
        assert update.callback_query.message.deleted is False
        assert "req" in context.user_data["pending_meanings"]

    asyncio.run(run())


def test_meaning_button_label_includes_option_number() -> None:
    assert meaning_button_label(0, "сдать") == "1. сдать"
    assert meaning_button_label(1, "сдавать") == "2. сдавать"


def test_meaning_target_text_prepends_article_once() -> None:
    assert meaning_target_text(MeaningOption(article="die", word="Karte")) == "die Karte"
    assert meaning_target_text(MeaningOption(article="die", word="die Karte")) == "die Karte"


def test_meaning_target_text_shows_phrase_target() -> None:
    meaning = MeaningOption(unit_type=UnitType.PHRASE, word="eine Prüfung bestehen", translation_ru="сдать экзамен")

    assert meaning_target_text(meaning) == "eine Prüfung bestehen"
    assert meaning_button_label(0, meaning_target_text(meaning)) == "1. eine Prüfung bestehen"


def test_meaning_option_text_shows_phrase_target_without_service_wording() -> None:
    meaning = MeaningOption(
        unit_type=UnitType.PHRASE,
        word="eine Prüfung bestehen",
        translation_ru="сдать экзамен",
        short_label="сдать экзамен",
        explanation_ru="Естественное словосочетание для успешной сдачи экзамена.",
    )

    text = meaning_option_text(1, meaning)

    assert "2. сдать экзамен" in text
    assert "Карточка: eine Prüfung bestehen" in text
    assert "Будет добавлено" not in text
    assert "(phrase)" not in text
    assert "Естественное словосочетание" in text


def test_meaning_target_text_shows_single_verb_when_selected() -> None:
    meaning = MeaningOption(unit_type=UnitType.WORD, word="bestehen", translation_ru="сдать")

    assert meaning_target_text(meaning) == "bestehen"
    assert meaning_button_label(0, meaning_target_text(meaning)) == "1. bestehen"


def test_handle_meaning_cancel_removes_pending_request() -> None:
    async def run() -> None:
        class FakeCallbackQuery:
            def __init__(self) -> None:
                self.edited_text = ""

            async def edit_message_text(self, text: str) -> None:
                self.edited_text = text

        class FakeUpdate:
            def __init__(self) -> None:
                self.callback_query = FakeCallbackQuery()
                self.effective_user = None

        class FakeContext:
            def __init__(self) -> None:
                self.user_data = {"pending_meanings": {"req": {"source_type": "text"}}}

        update = FakeUpdate()
        context = FakeContext()

        await handle_meaning_cancel_callback(update, context, "req")

        assert context.user_data["pending_meanings"] == {}
        assert update.callback_query.edited_text == "Выбор значения отменен."

    asyncio.run(run())


def test_add_callback_shows_anki_note_id_and_full_cost() -> None:
    async def run() -> None:
        draft = CardDraft(
            id=7,
            user_id=100000001,
            source_text="schweigen",
            polza_cost_rub=Decimal("4.451"),
            polza_cost_complete=True,
            polza_cost_available=True,
        )

        class FakeQuery:
            data = "add:7"

            def __init__(self) -> None:
                self.edited_text = ""
                self.message = type("FakeMessage", (), {"reply_text": _async_noop})()

            async def answer(self) -> None:
                return None

            async def edit_message_text(self, text: str) -> None:
                self.edited_text = text

        class FakeDrafts:
            def get(self, draft_id: int) -> CardDraft:
                assert draft_id == 7
                return draft

            def update_status(self, draft_id: int, status) -> None:
                return None

        class FakeAnki:
            async def add_card(self, card: CardDraft) -> int:
                return 1700000000000

        deps = type(
            "FakeDeps",
            (),
            {
                "allowed_user_ids": {100000001},
                "drafts": FakeDrafts(),
                "anki": FakeAnki(),
            },
        )()
        query = FakeQuery()
        update = type(
            "FakeUpdate",
            (),
            {
                "effective_user": type("FakeUser", (), {"id": 100000001})(),
                "callback_query": query,
            },
        )()
        context = type(
            "FakeContext",
            (),
            {
                "application": type(
                    "FakeApplication",
                    (),
                    {"bot_data": {"deps": deps}},
                )(),
            },
        )()

        await handle_callback(update, context)

        assert query.edited_text == (
            "Добавлено в Anki. Note ID: 1700000000000. Стоимость: 4,45 руб."
        )

    asyncio.run(run())


async def _async_noop(*args, **kwargs):
    return None
