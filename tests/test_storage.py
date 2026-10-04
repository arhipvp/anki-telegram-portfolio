import sqlite3
from decimal import Decimal

from anki_telegram_bot.models import CardDraft, DraftStatus, UnitType
from anki_telegram_bot.storage import DraftRepository


def test_repository_saves_and_reads_draft(tmp_path) -> None:
    repository = DraftRepository(tmp_path / "app.db")
    draft = CardDraft(
        user_id=42,
        language="ko",
        source_text="оформить страховку",
        unit_type=UnitType.PHRASE,
        word_or_phrase_de="eine Versicherung abschließen",
        word="Versicherung",
        article="die",
        translation_ru="оформить страховку",
        example_de="Ich möchte eine Versicherung abschließen.",
        example_ru="Я хочу оформить страховку.",
        sentence_de="Ich möchte <b>eine Versicherung abschließen</b>.",
        sentence_ru="Я хочу <b>оформить страховку</b>.",
        verb_forms="abschließen / schloss ab / hat abgeschlossen",
        image_query="insurance contract",
        audio_path=tmp_path / "voice.mp3",
        polza_cost_rub=Decimal("4.451"),
        polza_cost_complete=False,
        polza_cost_available=True,
    )

    saved = repository.save(draft)
    repository.update_status(saved.id, DraftStatus.REJECTED)
    loaded = repository.get(saved.id)

    assert loaded is not None
    assert loaded.status == DraftStatus.REJECTED
    assert loaded.word == "Versicherung"
    assert loaded.sentence_de == "Ich möchte <b>eine Versicherung abschließen</b>."
    assert loaded.verb_forms == "abschließen / schloss ab / hat abgeschlossen"
    assert loaded.image_query == "insurance contract"
    assert loaded.audio_path == tmp_path / "voice.mp3"
    assert loaded.polza_cost_rub == Decimal("4.451")
    assert loaded.language == "ko"
    assert loaded.polza_cost_complete is False
    assert loaded.polza_cost_available is True


def test_repository_migrates_existing_database_with_cost_defaults(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE card_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                source_text TEXT NOT NULL,
                unit_type TEXT NOT NULL,
                word_or_phrase_de TEXT NOT NULL,
                translation_ru TEXT NOT NULL,
                example_de TEXT NOT NULL,
                example_ru TEXT NOT NULL,
                frequency_band TEXT NOT NULL,
                learning_priority TEXT NOT NULL DEFAULT '',
                grammar_note_ru TEXT NOT NULL,
                image_path TEXT,
                image_source TEXT,
                audio_path TEXT,
                status TEXT NOT NULL
            )
            """
        )

    repository = DraftRepository(db_path)
    saved = repository.save(CardDraft(user_id=1, source_text="alt"))
    loaded = repository.get(saved.id)

    assert loaded is not None
    assert loaded.polza_cost_rub == Decimal("0")
    assert loaded.polza_cost_complete is True
    assert loaded.polza_cost_available is False
    assert loaded.verb_forms == ""


def test_repository_persists_current_language(tmp_path) -> None:
    repository = DraftRepository(tmp_path / "settings.db")

    assert repository.get_user_language(42, "de") == "de"
    repository.set_user_language(42, "ko")

    assert repository.get_user_language(42, "de") == "ko"
