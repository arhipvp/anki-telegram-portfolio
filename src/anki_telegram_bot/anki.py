import base64
import logging
import time
from pathlib import Path

import httpx

from .anki_format import FIELDS, format_for_language
from .logging_config import is_full_debug_enabled, log_event
from .models import CardDraft


logger = logging.getLogger(__name__)

REQUIRED_ANKI_FIELDS = [
    "SentenceDE",
    "Word",
    "TranslationRU",
    "SentenceRU",
    "MeaningRU",
    "DefinitionRU",
    "GrammarTable",
    "Level",
    "FrequencyBand",
    "SourceText",
]
ANKI_DUPLICATE_ERROR = "cannot create note because it is a duplicate"
ANKI_SYNC_WAIT_TIMEOUT_SECONDS = 180
LEGACY_N8N_NOTE_TYPE = "n8n v.2"
LEGACY_GERMAN_TTS = "{{tts de_DE:SentenceDE}}"
FIXED_GERMAN_TTS = "{{tts de_DE voices=com.google.android.tts-de-de-x-dea-local:SentenceDE}}"
ANKI_REQUEST_MAX_ATTEMPTS = 2
ANKI_RETRYABLE_ACTIONS = {
    "changeDeck",
    "createDeck",
    "findCards",
    "findNotes",
    "modelFieldAdd",
    "modelFieldNames",
    "modelNames",
    "modelTemplates",
    "notesInfo",
    "storeMediaFile",
    "sync",
    "syncAndWait",
    "syncAuthStatus",
    "updateModelStyling",
    "updateModelTemplates",
}
DEDUPLICATION_FIELDS = (
    "SentenceDE",
    "Word",
    "TranslationRU",
    "SentenceRU",
    "SourceText",
)


class AnkiConnectClient:
    def __init__(self, url: str, deck_name: str, note_type: str, language: str = "de") -> None:
        self.url = url
        self.deck_name = deck_name
        self.note_type = note_type
        self.language = language
        self.client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"Connection": "close"},
        )

    async def close(self) -> None:
        await self.client.aclose()
        log_event(logger, logging.INFO, "anki.client.closed", url=self.url)

    async def add_card(self, draft: CardDraft) -> int:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "anki.add_card.start", draft_id=draft.id, deck_name=self.deck_name, note_type=self.note_type, draft=draft if is_full_debug_enabled() else None)
        await self.ensure_deck()
        await self.ensure_note_type()
        image_html = ""
        if draft.image_path:
            filename = await self.store_media(draft.image_path)
            image_html = f'<img src="{filename}">'
        user_audio = ""
        if draft.audio_path:
            filename = await self.store_media(draft.audio_path)
            user_audio = f"[sound:{filename}]"
        fields = draft.anki_fields(image_html=image_html, user_audio=user_audio)
        validate_required_fields(fields)

        note = {
            "deckName": self.deck_name,
            "modelName": self.note_type,
            "fields": fields,
            "tags": ["telegram", "german" if self.language == "de" else self.language, draft.unit_type.value, draft.image_source.value if draft.image_source else "no_image"],
        }
        try:
            result = await self.request("addNote", {"note": note})
            note_id = int(result)
        except RuntimeError as exc:
            if not _is_duplicate_error(exc):
                raise
            log_event(
                logger,
                logging.WARNING,
                "anki.add_card.duplicate_detected",
                draft_id=draft.id,
                word=fields.get("Word"),
                source_text=fields.get("SourceText"),
            )
            note_id = await self.find_duplicate_note_id(fields)
        await self.move_note_cards(note_id)
        try:
            await self.sync()
        except (httpx.HTTPError, RuntimeError):
            log_event(
                logger,
                logging.ERROR,
                "anki.add_card.sync_error",
                draft_id=draft.id,
                note_id=note_id,
                exc_info=True,
            )
        log_event(
            logger,
            logging.INFO,
            "anki.add_card.success",
            draft_id=draft.id,
            note_id=note_id,
            duration_ms=_duration_ms(started_at),
        )
        return note_id

    async def find_duplicate_note_id(self, fields: dict[str, str]) -> int:
        word = fields.get("Word", "").strip()
        if not word:
            raise RuntimeError("AnkiConnect addNote failed: duplicate without Word field")
        note_ids = await self.request("findNotes", {"query": f'"{_escape_anki_query(word)}"'})
        if not isinstance(note_ids, list) or not note_ids:
            log_event(
                logger,
                logging.ERROR,
                "anki.add_card.duplicate_not_found",
                word=word,
                note_ids=note_ids,
            )
            raise RuntimeError("AnkiConnect addNote failed: duplicate note was not found")
        notes = await self.request("notesInfo", {"notes": note_ids})
        matches = [
            int(note["noteId"])
            for note in notes or []
            if _note_matches_fields(note, self.note_type, fields)
        ]
        if not matches:
            log_event(
                logger,
                logging.ERROR,
                "anki.add_card.duplicate_not_found",
                word=word,
                note_ids=note_ids,
                checked_count=len(notes or []),
            )
            raise RuntimeError("AnkiConnect addNote failed: duplicate note was not found")
        if len(matches) > 1:
            log_event(
                logger,
                logging.ERROR,
                "anki.add_card.duplicate_ambiguous",
                word=word,
                matching_note_ids=matches,
            )
            raise RuntimeError("AnkiConnect addNote failed: duplicate note is ambiguous")
        note_id = matches[0]
        log_event(
            logger,
            logging.INFO,
            "anki.add_card.duplicate_recovered",
            note_id=note_id,
            word=word,
        )
        return note_id

    async def move_note_cards(self, note_id: int) -> None:
        card_ids = await self.request("findCards", {"query": f"nid:{note_id}"})
        if not card_ids:
            return
        await self.request("changeDeck", {"cards": card_ids, "deck": self.deck_name})
        log_event(
            logger,
            logging.INFO,
            "anki.cards.moved",
            note_id=note_id,
            card_ids=card_ids,
            deck_name=self.deck_name,
        )

    async def sync(self) -> None:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "anki.sync.start")
        authenticated = await self.request("syncAuthStatus", {})
        if authenticated is not True:
            raise RuntimeError(
                "AnkiWeb login required. Open http://127.0.0.1:3000 and sign in once."
            )
        try:
            await self.request("syncAndWait", {"timeoutSeconds": ANKI_SYNC_WAIT_TIMEOUT_SECONDS})
        except RuntimeError as exc:
            if "unsupported action" not in str(exc).casefold():
                raise
            log_event(
                logger,
                logging.WARNING,
                "anki.sync.wait_action_unavailable",
                fallback_action="sync",
            )
            await self.request("sync", {})
        log_event(logger, logging.INFO, "anki.sync.success", duration_ms=_duration_ms(started_at))

    async def ensure_deck(self) -> None:
        log_event(logger, logging.INFO, "anki.ensure_deck.start", deck_name=self.deck_name)
        await self.request("createDeck", {"deck": self.deck_name})
        log_event(logger, logging.INFO, "anki.ensure_deck.success", deck_name=self.deck_name)

    async def ensure_note_type(self) -> None:
        log_event(logger, logging.INFO, "anki.ensure_note_type.start", note_type=self.note_type)
        model_names = await self.request("modelNames", {})
        if self.note_type in model_names:
            await self.ensure_note_type_fields()
            await self.update_note_type_templates()
            log_event(logger, logging.INFO, "anki.ensure_note_type.exists", note_type=self.note_type)
            return
        await self.request(
            "createModel",
            {
                "modelName": self.note_type,
                "inOrderFields": format_for_language(self.language)[1],
                "css": format_for_language(self.language)[2],
                "cardTemplates": format_for_language(self.language)[3],
            },
        )
        log_event(logger, logging.INFO, "anki.ensure_note_type.created", note_type=self.note_type, fields=FIELDS)

    async def ensure_legacy_n8n_tts(self) -> None:
        model_names = await self.request("modelNames", {})
        if LEGACY_N8N_NOTE_TYPE not in model_names:
            return
        templates = await self.request("modelTemplates", {"modelName": LEGACY_N8N_NOTE_TYPE})
        changed = False
        for template in templates.values():
            for side in ("Front", "Back"):
                content = template.get(side, "")
                updated = content.replace(LEGACY_GERMAN_TTS, FIXED_GERMAN_TTS)
                if updated != content:
                    template[side] = updated
                    changed = True
        if changed:
            await self.request(
                "updateModelTemplates",
                {"model": {"name": LEGACY_N8N_NOTE_TYPE, "templates": templates}},
            )
            log_event(logger, logging.INFO, "anki.legacy_n8n_tts.updated", note_type=LEGACY_N8N_NOTE_TYPE)

    async def ensure_note_type_fields(self) -> None:
        existing_fields = await self.request("modelFieldNames", {"modelName": self.note_type})
        missing_fields = [field for field in FIELDS if field not in existing_fields]
        added_fields: list[str] = []
        for field in missing_fields:
            await self.request("modelFieldAdd", {"modelName": self.note_type, "fieldName": field})
            added_fields.append(field)
        log_event(
            logger,
            logging.INFO,
            "anki.ensure_note_type.fields",
            note_type=self.note_type,
            missing_fields=missing_fields,
            added_fields=added_fields,
        )

    async def update_note_type_templates(self) -> None:
        _note_type_name, _fields, css, card_templates = format_for_language(self.language)
        templates = {
            template["Name"]: {
                "Front": template["Front"],
                "Back": template["Back"],
            }
            for template in card_templates
        }
        await self.request("updateModelTemplates", {"model": {"name": self.note_type, "templates": templates}})
        await self.request("updateModelStyling", {"model": {"name": self.note_type, "css": css}})
        log_event(logger, logging.INFO, "anki.ensure_note_type.templates_updated", note_type=self.note_type)

    async def store_media(self, path: Path) -> str:
        raw_bytes = path.read_bytes()
        payload = base64.b64encode(raw_bytes).decode("ascii")
        filename = path.name
        log_event(logger, logging.INFO, "anki.store_media.start", filename=filename, path=path, size_bytes=len(raw_bytes))
        await self.request("storeMediaFile", {"filename": filename, "data": payload})
        log_event(logger, logging.INFO, "anki.store_media.success", filename=filename, size_bytes=len(raw_bytes))
        return filename

    async def request(self, action: str, params: dict) -> object:
        started_at = time.perf_counter()
        logged_params = _params_for_log(action, params)
        log_event(logger, logging.INFO, "anki.request.start", action=action, url=self.url, params=logged_params if is_full_debug_enabled() else None)
        attempt = 1
        while True:
            try:
                response = await self.client.post(self.url, json={"action": action, "version": 6, "params": params})
                break
            except httpx.TransportError as exc:
                if action in ANKI_RETRYABLE_ACTIONS and attempt < ANKI_REQUEST_MAX_ATTEMPTS:
                    log_event(
                        logger,
                        logging.WARNING,
                        "anki.request.retry",
                        action=action,
                        attempt=attempt,
                        max_attempts=ANKI_REQUEST_MAX_ATTEMPTS,
                        exception_type=type(exc).__name__,
                        duration_ms=_duration_ms(started_at),
                    )
                    attempt += 1
                    continue
                log_event(
                    logger,
                    logging.ERROR,
                    "anki.request.transport_error",
                    action=action,
                    attempt=attempt,
                    exception_type=type(exc).__name__,
                    duration_ms=_duration_ms(started_at),
                    params=logged_params if is_full_debug_enabled() else None,
                )
                raise
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            log_event(
                logger,
                logging.ERROR,
                "anki.request.error",
                action=action,
                status_code=response.status_code,
                error=data["error"],
                duration_ms=_duration_ms(started_at),
                params=logged_params if is_full_debug_enabled() else None,
            )
            raise RuntimeError(f"AnkiConnect {action} failed: {data['error']}")
        log_event(
            logger,
            logging.INFO,
            "anki.request.success",
            action=action,
            status_code=response.status_code,
            result=data.get("result") if is_full_debug_enabled() else _result_summary(data.get("result")),
            duration_ms=_duration_ms(started_at),
        )
        return data.get("result")


def _params_for_log(action: str, params: dict) -> dict:
    if action != "storeMediaFile":
        return params
    data = params.get("data", "")
    return {
        **params,
        "data": f"***BASE64_MEDIA_REDACTED:{len(data)} chars***",
    }


def _result_summary(result: object) -> object:
    if isinstance(result, list):
        return {"type": "list", "count": len(result)}
    if isinstance(result, dict):
        return {"type": "dict", "keys": sorted(result.keys())}
    return result


def validate_required_fields(fields: dict[str, str]) -> None:
    missing = [field for field in REQUIRED_ANKI_FIELDS if not fields.get(field)]
    if missing:
        log_event(logger, logging.WARNING, "anki.required_fields.missing", missing_fields=missing)
        raise ValueError(f"Missing required Anki fields: {', '.join(missing)}")


def _is_duplicate_error(exc: RuntimeError) -> bool:
    return ANKI_DUPLICATE_ERROR in str(exc).casefold()


def _escape_anki_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _note_matches_fields(note: dict, note_type: str, fields: dict[str, str]) -> bool:
    if note.get("modelName") != note_type:
        return False
    note_fields = note.get("fields") or {}
    for field_name in DEDUPLICATION_FIELDS:
        if _note_field_value(note_fields, field_name) != fields.get(field_name, ""):
            return False
    return True


def _note_field_value(note_fields: dict, field_name: str) -> str:
    value = note_fields.get(field_name, "")
    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def _duration_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)
