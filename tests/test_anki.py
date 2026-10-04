import asyncio

import httpx
import pytest

from anki_telegram_bot.anki import AnkiConnectClient, validate_required_fields
from anki_telegram_bot.anki_format import CARD_TEMPLATES, FIELDS, NOTE_TYPE_NAME, format_for_language
from anki_telegram_bot.models import CardDraft, ImageSource, UnitType


def test_card_templates_use_fixed_german_tts_voice() -> None:
    expected_tts = "{{tts de_DE voices=com.google.android.tts-de-de-x-dea-local:SentenceDE}}"

    template_text = "\n".join(
        template_part
        for template in CARD_TEMPLATES
        for template_part in (template["Front"], template["Back"])
    )

    assert template_text.count(expected_tts) == 3
    assert "{{tts de_DE:SentenceDE}}" not in template_text


def test_language_formats_include_english_and_korean_tts() -> None:
    english_name, _, _, english_templates = format_for_language("en")
    korean_name, _, _, korean_templates = format_for_language("ko")

    assert english_name == "English Context v1"
    assert korean_name == "Korean Context v1"
    assert "{{tts en_US:SentenceDE}}" in english_templates[0]["Front"]
    assert "{{tts ko_KR:SentenceDE}}" in korean_templates[0]["Front"]
    assert "{{tts de_DE voices=" not in CARD_TEMPLATES[1]["Front"]


def test_add_card_maps_german_context_fields(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "modelNames":
                return [NOTE_TYPE_NAME]
            if action == "syncAuthStatus":
                return True
            if action == "addNote":
                return 123
            if action == "findCards":
                return [456, 457]
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        draft = CardDraft(
            user_id=1,
            source_text="страховка",
            unit_type=UnitType.WORD,
            word="Versicherung",
            article="die",
            gender_color="f",
            level="B1",
            translation_ru="страховка",
            sentence_de="Ich brauche <b>eine Versicherung</b>.",
            sentence_ru="Мне нужна <b>страховка</b>.",
            meaning_ru="договор защиты от риска",
            definition_ru="Eine Versicherung schützt vor Risiken.",
            grammar_table="<ul><li>die Versicherung</li></ul>",
            verb_forms="",
            image_query="insurance contract",
        )

        note_id = await client.add_card(draft)
        await client.close()

        assert note_id == 123
        add_note = next(params for action, params in calls if action == "addNote")
        note = add_note["note"]
        assert note["modelName"] == NOTE_TYPE_NAME
        assert note["fields"]["Word"] == "Versicherung"
        assert note["fields"]["Article"] == "die"
        assert note["fields"]["GenderColor"] == "f"
        assert note["fields"]["SentenceDE"] == "Ich brauche <b>eine Versicherung</b>."
        assert note["fields"]["SentenceRU"] == "Мне нужна <b>страховка</b>."
        assert note["fields"]["ImageQuery"] == "insurance contract"
        assert note["fields"]["VerbForms"] == ""
        assert note["fields"]["FrequencyBand"] == "medium"
        assert note["fields"]["SourceText"] == "страховка"
        assert note["fields"]["UserAudio"] == ""
        assert "learning_priority" not in note["fields"]
        assert note["tags"] == ["telegram", "german", "word", "no_image"]
        change_deck = next(params for action, params in calls if action == "changeDeck")
        assert change_deck == {"cards": [456, 457], "deck": "German"}
        assert [action for action, _ in calls if action in {"addNote", "findCards", "changeDeck", "syncAuthStatus", "syncAndWait", "sync"}] == [
            "addNote",
            "findCards",
            "changeDeck",
            "syncAuthStatus",
            "syncAndWait",
        ]

    asyncio.run(run())


def test_ensure_note_type_creates_german_context_model(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "modelNames":
                return []
            return None

        monkeypatch.setattr(client, "request", fake_request)

        await client.ensure_note_type()
        await client.close()

        create_model = next(params for action, params in calls if action == "createModel")
        assert create_model["modelName"] == NOTE_TYPE_NAME
        assert create_model["inOrderFields"] == FIELDS
        assert len(create_model["cardTemplates"]) == 2
        assert create_model["cardTemplates"] == CARD_TEMPLATES
        assert "{{#SentenceRU}}" in create_model["cardTemplates"][1]["Front"]
        assert "{{#OtherMeaningsRU}}" in create_model["cardTemplates"][1]["Back"]
        assert "FrequencyBand" in create_model["inOrderFields"]
        assert "SourceText" in create_model["inOrderFields"]
        assert "UserAudio" in create_model["inOrderFields"]
        assert "VerbForms" in create_model["inOrderFields"]
        assert "{{#VerbForms}} / {{VerbForms}}{{/VerbForms}}" in create_model["cardTemplates"][0]["Back"]
        assert "{{#FrequencyBand}}" in create_model["cardTemplates"][0]["Back"]
        assert "{{#SourceText}}" in create_model["cardTemplates"][0]["Back"]
        assert "{{#UserAudio}}" in create_model["cardTemplates"][0]["Back"]

    asyncio.run(run())


def test_ensure_note_type_updates_existing_model(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "modelNames":
                return [NOTE_TYPE_NAME]
            if action == "modelFieldNames":
                return [field for field in FIELDS if field not in {"FrequencyBand", "SourceText", "VerbForms"}]
            return None

        monkeypatch.setattr(client, "request", fake_request)

        await client.ensure_note_type()
        await client.close()

        added_fields = [params["fieldName"] for action, params in calls if action == "modelFieldAdd"]
        assert added_fields == ["VerbForms", "FrequencyBand", "SourceText"]
        update_templates = next(params for action, params in calls if action == "updateModelTemplates")
        expected_templates = {
            template["Name"]: {"Front": template["Front"], "Back": template["Back"]}
            for template in CARD_TEMPLATES
        }
        assert update_templates == {
            "model": {"name": NOTE_TYPE_NAME, "templates": expected_templates}
        }
        assert any(action == "updateModelStyling" for action, _ in calls)

    asyncio.run(run())


def test_ensure_legacy_n8n_tts_updates_old_templates(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "modelNames":
                return ["n8n v.2"]
            if action == "modelTemplates":
                return {
                    "Card 1": {"Front": "{{tts de_DE:SentenceDE}}", "Back": "{{tts de_DE:SentenceDE}}"},
                    "Card 2": {"Front": "", "Back": "{{tts de_DE:SentenceDE}}"},
                }
            return None

        monkeypatch.setattr(client, "request", fake_request)
        await client.ensure_legacy_n8n_tts()
        await client.close()

        update = next(params for action, params in calls if action == "updateModelTemplates")
        assert update["model"]["name"] == "n8n v.2"
        assert all(
            "{{tts de_DE voices=com.google.android.tts-de-de-x-dea-local:SentenceDE}}" in template[side]
            for template in update["model"]["templates"].values()
            for side in ("Front", "Back")
            if template[side]
        )

    asyncio.run(run())


def test_required_field_validation_allows_empty_optional_fields() -> None:
    fields = {
        "SentenceDE": "Ich brauche <b>eine Versicherung</b>.",
        "Word": "Versicherung",
        "Article": "",
        "GenderColor": "",
        "Level": "B1",
        "TranslationRU": "страховка",
        "SentenceRU": "Мне нужна <b>страховка</b>.",
        "Image": "",
        "MeaningRU": "договор защиты от риска",
        "OtherMeaningsRU": "",
        "DefinitionRU": "Eine Versicherung schützt vor Risiken.",
        "Synonyms": "",
        "GrammarTable": "<ul><li>die Versicherung</li></ul>",
        "Etymology": "",
        "StyleAndGroup": "",
        "ImageQuery": "",
        "FrequencyBand": "medium",
        "SourceText": "страховка",
    }

    validate_required_fields(fields)


def test_required_field_validation_rejects_missing_required_field() -> None:
    fields = {
        "SentenceDE": "",
        "Word": "Versicherung",
        "TranslationRU": "страховка",
        "SentenceRU": "Мне нужна <b>страховка</b>.",
        "MeaningRU": "договор защиты от риска",
        "DefinitionRU": "Eine Versicherung schützt vor Risiken.",
        "GrammarTable": "<ul><li>die Versicherung</li></ul>",
        "Level": "B1",
        "FrequencyBand": "medium",
        "SourceText": "страховка",
    }

    with pytest.raises(ValueError, match="SentenceDE"):
        validate_required_fields(fields)


def test_add_card_tags_user_photo_source(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "addNote":
                return 123
            if action == "syncAuthStatus":
                return True
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        draft = CardDraft(
            user_id=1,
            source_text="Фото",
            unit_type=UnitType.WORD,
            word="Tasse",
            level="A1",
            translation_ru="чашка",
            sentence_de="Die <b>Tasse</b> steht auf dem Tisch.",
            sentence_ru="<b>Чашка</b> стоит на столе.",
            meaning_ru="Gefäß zum Trinken.",
            definition_ru="Ein kleines Gefäß für Getränke.",
            grammar_table="<ul><li>die Tasse</li></ul>",
            image_source=ImageSource.TELEGRAM_PHOTO,
        )

        await client.add_card(draft)
        await client.close()

        add_note = next(params for action, params in calls if action == "addNote")
        assert add_note["note"]["tags"] == ["telegram", "german", "word", "telegram_photo"]
        assert [action for action, _ in calls if action in {"addNote", "syncAuthStatus", "syncAndWait", "sync"}] == [
            "addNote",
            "syncAuthStatus",
            "syncAndWait",
        ]

    asyncio.run(run())


def test_add_card_stores_user_audio(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        audio_path = tmp_path / "voice.mp3"
        audio_path.write_bytes(b"abc")
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "addNote":
                return 123
            if action == "syncAuthStatus":
                return True
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        draft = CardDraft(
            user_id=1,
            source_text="страховка",
            unit_type=UnitType.WORD,
            word="Versicherung",
            level="B1",
            translation_ru="страховка",
            sentence_de="Ich brauche <b>eine Versicherung</b>.",
            sentence_ru="Мне нужна <b>страховка</b>.",
            meaning_ru="договор защиты от риска",
            definition_ru="Eine Versicherung schützt vor Risiken.",
            grammar_table="<ul><li>die Versicherung</li></ul>",
            audio_path=audio_path,
        )

        await client.add_card(draft)
        await client.close()

        media_call = next(params for action, params in calls if action == "storeMediaFile")
        add_note = next(params for action, params in calls if action == "addNote")
        assert media_call["filename"] == "voice.mp3"
        assert add_note["note"]["fields"]["UserAudio"] == "[sound:voice.mp3]"

    asyncio.run(run())


def test_add_card_does_not_fail_when_sync_fails(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "addNote":
                return 123
            if action == "syncAuthStatus":
                return True
            if action == "syncAndWait":
                raise RuntimeError("AnkiWeb sync failed")
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        draft = CardDraft(
            user_id=1,
            source_text="страховка",
            unit_type=UnitType.WORD,
            word="Versicherung",
            level="B1",
            translation_ru="страховка",
            sentence_de="Ich brauche <b>eine Versicherung</b>.",
            sentence_ru="Мне нужна <b>страховка</b>.",
            meaning_ru="договор защиты от риска",
            definition_ru="Eine Versicherung schützt vor Risiken.",
            grammar_table="<ul><li>die Versicherung</li></ul>",
        )

        note_id = await client.add_card(draft)
        await client.close()

        assert note_id == 123
        assert [action for action, _ in calls if action in {"addNote", "syncAndWait", "sync"}] == ["addNote", "syncAndWait"]

    asyncio.run(run())


def test_add_card_recovers_existing_duplicate_note(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []
        draft = CardDraft(
            user_id=1,
            source_text="inzwischen",
            unit_type=UnitType.WORD,
            word="inzwischen",
            level="A2",
            translation_ru="тем временем",
            sentence_de="Ich koche, <b>inzwischen</b> deckt er den Tisch.",
            sentence_ru="Я готовлю, а он <b>тем временем</b> накрывает на стол.",
            meaning_ru="В то же самое время.",
            definition_ru="In der Zwischenzeit.",
            grammar_table="<ul><li>Temporaladverb</li></ul>",
        )
        expected_fields = draft.anki_fields()

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "addNote":
                raise RuntimeError("AnkiConnect addNote failed: cannot create note because it is a duplicate")
            if action == "findNotes":
                return [123]
            if action == "notesInfo":
                return [_note_info(123, expected_fields)]
            if action == "findCards":
                return [456, 457]
            if action == "syncAuthStatus":
                return True
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        note_id = await client.add_card(draft)
        await client.close()

        assert note_id == 123
        assert next(params for action, params in calls if action == "findNotes") == {"query": '"inzwischen"'}
        assert next(params for action, params in calls if action == "changeDeck") == {
            "cards": [456, 457],
            "deck": "German",
        }
        assert [action for action, _ in calls if action in {"addNote", "findNotes", "notesInfo", "findCards", "changeDeck", "syncAuthStatus", "syncAndWait", "sync"}] == [
            "addNote",
            "findNotes",
            "notesInfo",
            "findCards",
            "changeDeck",
            "syncAuthStatus",
            "syncAndWait",
        ]

    asyncio.run(run())


def test_add_card_duplicate_mismatch_stays_failed(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        draft = CardDraft(
            user_id=1,
            source_text="inzwischen",
            unit_type=UnitType.WORD,
            word="inzwischen",
            level="A2",
            translation_ru="тем временем",
            sentence_de="Ich koche, <b>inzwischen</b> deckt er den Tisch.",
            sentence_ru="Я готовлю, а он <b>тем временем</b> накрывает на стол.",
            meaning_ru="В то же самое время.",
            definition_ru="In der Zwischenzeit.",
            grammar_table="<ul><li>Temporaladverb</li></ul>",
        )
        mismatched_fields = draft.anki_fields()
        mismatched_fields["SentenceDE"] = "Andere Karte."

        async def fake_request(action: str, params: dict) -> object:
            if action == "addNote":
                raise RuntimeError("AnkiConnect addNote failed: cannot create note because it is a duplicate")
            if action == "findNotes":
                return [123]
            if action == "notesInfo":
                return [_note_info(123, mismatched_fields)]
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        with pytest.raises(RuntimeError, match="duplicate note was not found"):
            await client.add_card(draft)
        await client.close()

    asyncio.run(run())


def test_add_card_recovers_after_previous_change_deck_read_error(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []
        add_attempt = 0
        draft = CardDraft(
            user_id=1,
            source_text="inzwischen",
            unit_type=UnitType.WORD,
            word="inzwischen",
            level="A2",
            translation_ru="тем временем",
            sentence_de="Ich koche, <b>inzwischen</b> deckt er den Tisch.",
            sentence_ru="Я готовлю, а он <b>тем временем</b> накрывает на стол.",
            meaning_ru="В то же самое время.",
            definition_ru="In der Zwischenzeit.",
            grammar_table="<ul><li>Temporaladverb</li></ul>",
        )
        expected_fields = draft.anki_fields()

        async def fake_request(action: str, params: dict) -> object:
            nonlocal add_attempt
            calls.append((action, params))
            if action == "addNote":
                add_attempt += 1
                if add_attempt == 1:
                    return 123
                raise RuntimeError("AnkiConnect addNote failed: cannot create note because it is a duplicate")
            if action == "findCards":
                return [456, 457]
            if action == "changeDeck" and add_attempt == 1:
                raise httpx.ReadError("connection dropped")
            if action == "findNotes":
                return [123]
            if action == "notesInfo":
                return [_note_info(123, expected_fields)]
            if action == "syncAuthStatus":
                return True
            return None

        monkeypatch.setattr(client, "request", fake_request)
        monkeypatch.setattr(client, "ensure_deck", lambda: fake_request("createDeck", {"deck": "German"}))
        monkeypatch.setattr(client, "ensure_note_type", lambda: fake_request("modelNames", {}))

        with pytest.raises(httpx.ReadError):
            await client.add_card(draft)
        note_id = await client.add_card(draft)
        await client.close()

        assert note_id == 123
        assert add_attempt == 2
        change_deck_calls = [params for action, params in calls if action == "changeDeck"]
        assert change_deck_calls == [
            {"cards": [456, 457], "deck": "German"},
            {"cards": [456, 457], "deck": "German"},
        ]

    asyncio.run(run())


def test_sync_fails_before_triggering_sync_when_ankiweb_login_is_missing(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "syncAuthStatus":
                return False
            return None

        monkeypatch.setattr(client, "request", fake_request)

        with pytest.raises(RuntimeError, match="AnkiWeb login required"):
            await client.sync()
        await client.close()

        assert calls == [("syncAuthStatus", {})]

    asyncio.run(run())


def test_sync_falls_back_to_legacy_sync_when_wait_action_is_missing(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = []

        async def fake_request(action: str, params: dict) -> object:
            calls.append((action, params))
            if action == "syncAuthStatus":
                return True
            if action == "syncAndWait":
                raise RuntimeError("AnkiConnect syncAndWait failed: unsupported action")
            return None

        monkeypatch.setattr(client, "request", fake_request)

        await client.sync()
        await client.close()

        assert calls == [
            ("syncAuthStatus", {}),
            ("syncAndWait", {"timeoutSeconds": 180}),
            ("sync", {}),
        ]

    asyncio.run(run())


def test_request_retries_retryable_transport_error(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = 0

        async def fake_post(url: str, json: dict) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadError("connection dropped")
            request = httpx.Request("POST", url)
            return httpx.Response(200, json={"result": None, "error": None}, request=request)

        monkeypatch.setattr(client.client, "post", fake_post)

        assert await client.request("changeDeck", {"cards": [123], "deck": "German"}) is None
        await client.close()

        assert calls == 2

    asyncio.run(run())


def test_request_does_not_retry_add_note_transport_error(monkeypatch) -> None:
    async def run() -> None:
        client = AnkiConnectClient("http://127.0.0.1:8765", "German", NOTE_TYPE_NAME)
        calls = 0

        async def fake_post(url: str, json: dict) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadError("connection dropped")

        monkeypatch.setattr(client.client, "post", fake_post)

        with pytest.raises(httpx.ReadError):
            await client.request("addNote", {"note": {}})
        await client.close()

        assert calls == 1

    asyncio.run(run())


def _note_info(note_id: int, fields: dict[str, str], model_name: str = NOTE_TYPE_NAME) -> dict:
    return {
        "noteId": note_id,
        "modelName": model_name,
        "fields": {
            field_name: {"value": value, "order": index}
            for index, (field_name, value) in enumerate(fields.items())
        },
    }
