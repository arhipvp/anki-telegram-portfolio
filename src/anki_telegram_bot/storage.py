import logging
import sqlite3
from pathlib import Path
from typing import Any

from .logging_config import is_full_debug_enabled, log_event
from .models import CardDraft, DraftStatus


logger = logging.getLogger(__name__)


BASE_COLUMNS = [
    "user_id",
    "language",
    "source_text",
    "unit_type",
    "word_or_phrase_de",
    "translation_ru",
    "example_de",
    "example_ru",
    "frequency_band",
    "grammar_note_ru",
    "image_path",
    "image_source",
    "audio_path",
    "status",
]

GERMAN_CONTEXT_COLUMNS = [
    "sentence_de",
    "word",
    "article",
    "gender_color",
    "level",
    "sentence_ru",
    "meaning_ru",
    "other_meanings_ru",
    "definition_ru",
    "synonyms",
    "grammar_table",
    "verb_forms",
    "etymology",
    "style_and_group",
    "image_query",
]

LEGACY_COLUMNS = ["learning_priority"]
COST_COLUMNS = {
    "polza_cost_rub": "TEXT NOT NULL DEFAULT '0'",
    "polza_cost_complete": "INTEGER NOT NULL DEFAULT 1",
    "polza_cost_available": "INTEGER NOT NULL DEFAULT 0",
}
COLUMNS = [*BASE_COLUMNS, *GERMAN_CONTEXT_COLUMNS, *COST_COLUMNS]
WRITE_COLUMNS = [*COLUMNS, *LEGACY_COLUMNS]
OPTIONAL_BASE_COLUMNS = ["audio_path"]
USER_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL
)
"""


class DraftRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        log_event(logger, logging.INFO, "storage.repository.init", db_path=self.db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        log_event(logger, logging.INFO, "storage.schema.init.start", db_path=self.db_path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS card_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    language TEXT NOT NULL DEFAULT 'de',
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
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(card_drafts)").fetchall()}
            conn.execute(USER_SETTINGS_SCHEMA)
            if "language" not in existing:
                conn.execute("ALTER TABLE card_drafts ADD COLUMN language TEXT NOT NULL DEFAULT 'de'")
            for column in LEGACY_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE card_drafts ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                    log_event(logger, logging.INFO, "storage.schema.legacy_column_added", db_path=self.db_path, column=column)
            for column in OPTIONAL_BASE_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE card_drafts ADD COLUMN {column} TEXT")
                    log_event(logger, logging.INFO, "storage.schema.column_added", db_path=self.db_path, column=column)
            for column, definition in COST_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE card_drafts ADD COLUMN {column} {definition}")
                    log_event(logger, logging.INFO, "storage.schema.column_added", db_path=self.db_path, column=column)
            for column in GERMAN_CONTEXT_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE card_drafts ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                    log_event(logger, logging.INFO, "storage.schema.column_added", db_path=self.db_path, column=column)
        log_event(logger, logging.INFO, "storage.schema.init.success", db_path=self.db_path)

    def save(self, draft: CardDraft) -> CardDraft:
        data = draft.model_dump(mode="json")
        data["image_path"] = str(draft.image_path) if draft.image_path else None
        data["audio_path"] = str(draft.audio_path) if draft.audio_path else None
        for column in LEGACY_COLUMNS:
            data[column] = ""
        values = [data[column] for column in WRITE_COLUMNS]

        with self._connect() as conn:
            if draft.id is None:
                log_event(logger, logging.INFO, "storage.draft.insert.start", db_path=self.db_path, user_id=draft.user_id, status=draft.status, draft=draft if is_full_debug_enabled() else None)
                cursor = conn.execute(
                    f"INSERT INTO card_drafts ({', '.join(WRITE_COLUMNS)}) VALUES ({', '.join(['?'] * len(WRITE_COLUMNS))})",
                    values,
                )
                saved = draft.model_copy(update={"id": int(cursor.lastrowid)})
                log_event(logger, logging.INFO, "storage.draft.insert.success", db_path=self.db_path, draft_id=saved.id, user_id=saved.user_id, status=saved.status, draft=saved if is_full_debug_enabled() else None)
                return saved

            log_event(logger, logging.INFO, "storage.draft.update.start", db_path=self.db_path, draft_id=draft.id, user_id=draft.user_id, status=draft.status, draft=draft if is_full_debug_enabled() else None)
            conn.execute(
                f"UPDATE card_drafts SET {', '.join(f'{column} = ?' for column in WRITE_COLUMNS)} WHERE id = ?",
                [*values, draft.id],
            )
            log_event(logger, logging.INFO, "storage.draft.update.success", db_path=self.db_path, draft_id=draft.id, user_id=draft.user_id, status=draft.status)
            return draft

    def get(self, draft_id: int) -> CardDraft | None:
        log_event(logger, logging.INFO, "storage.draft.get.start", db_path=self.db_path, draft_id=draft_id)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM card_drafts WHERE id = ?", (draft_id,)).fetchone()
        draft = self._from_row(row) if row else None
        log_event(logger, logging.INFO, "storage.draft.get.success", db_path=self.db_path, draft_id=draft_id, found=bool(draft), draft=draft if draft and is_full_debug_enabled() else None)
        return draft

    def latest_for_user(self, user_id: int) -> CardDraft | None:
        log_event(logger, logging.INFO, "storage.draft.latest.start", db_path=self.db_path, user_id=user_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM card_drafts WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        draft = self._from_row(row) if row else None
        log_event(logger, logging.INFO, "storage.draft.latest.success", db_path=self.db_path, user_id=user_id, found=bool(draft), draft_id=draft.id if draft else None, draft=draft if draft and is_full_debug_enabled() else None)
        return draft

    def update_status(self, draft_id: int, status: DraftStatus) -> None:
        log_event(logger, logging.INFO, "storage.draft.status_update.start", db_path=self.db_path, draft_id=draft_id, status=status)
        with self._connect() as conn:
            conn.execute("UPDATE card_drafts SET status = ? WHERE id = ?", (status.value, draft_id))
        log_event(logger, logging.INFO, "storage.draft.status_update.success", db_path=self.db_path, draft_id=draft_id, status=status)

    def get_user_language(self, user_id: int, default: str = "de") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT language FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return str(row["language"]) if row else default

    def set_user_language(self, user_id: int, language: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO user_settings (user_id, language) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET language = excluded.language",
                (user_id, language),
            )

    def _from_row(self, row: sqlite3.Row) -> CardDraft:
        data: dict[str, Any] = dict(row)
        if data.get("image_path"):
            data["image_path"] = Path(data["image_path"])
        if data.get("audio_path"):
            data["audio_path"] = Path(data["audio_path"])
        for column in LEGACY_COLUMNS:
            data.pop(column, None)
        return CardDraft.model_validate(data)
