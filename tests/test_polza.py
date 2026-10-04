import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from anki_telegram_bot.polza import (
    CARD_SYSTEM_PROMPT,
    PolzaClient,
    audio_path_to_data_url,
    card_json_schema,
    extract_image_url,
    extract_media_id,
    image_path_to_data_url,
    parse_audio_meaning_selection,
    parse_json_object,
    parse_meaning_options,
    parse_meaning_selection,
    validate_generated_draft,
)
from anki_telegram_bot.models import CardDraft, MeaningOption, UnitType


RICH_NOUN_GRAMMAR = (
    "<ul><li><b>Формы:</b> die Einrichtung, Genitiv der Einrichtung, Plural die Einrichtungen.</li>"
    "<li><b>Управление:</b> Einrichtung von + Dativ; Einrichtung für + Akkusativ.</li>"
    "<li><b>Коллокации:</b> moderne Einrichtung, technische Einrichtung, öffentliche Einrichtung.</li>"
    "<li><b>Ошибка:</b> не путать Einrichtung как обстановку с einzelnen Möbeln.</li></ul>"
)
RICH_PHRASE_GRAMMAR = (
    "<ul><li><b>Структура:</b> Akkusativobjekt + gewinnen; объект стоит перед инфинитивом.</li>"
    "<li><b>Формы:</b> gewinnt, gewann, hat gewonnen; глагол сильный и неотделяемый.</li>"
    "<li><b>Коллокации:</b> neue Kunden gewinnen, Vertrauen gewinnen, Zeit gewinnen.</li>"
    "<li><b>Ошибка:</b> для привлечения клиентов не использовать bekommen без нужного контекста.</li></ul>"
)
RICH_VERB_GRAMMAR = (
    "<ul><li><b>Управление:</b> vereinbaren + Akkusativ; vereinbaren mit + Dativ.</li>"
    "<li><b>Формы:</b> vereinbaren, vereinbarte, hat vereinbart; неотделяемый, невозвратный.</li>"
    "<li><b>Коллокации:</b> einen Termin vereinbaren, ein Treffen vereinbaren, Regeln vereinbaren.</li>"
    "<li><b>Ошибка:</b> не путать с sich vertragen, когда речь о личном примирении.</li></ul>"
)


def rich_card_json(**updates: object) -> str:
    data: dict[str, object] = {
        "unit_type": "word",
        "word": "Einrichtung",
        "article": "die",
        "gender_color": "f",
        "level": "A2",
        "translation_ru": "обстановка",
        "sentence_de": "Die <b>Einrichtung</b> ist modern.",
        "sentence_ru": "<b>Обстановка</b> современная.",
        "meaning_ru": "Мебель и предметы, которыми обустроено помещение.",
        "other_meanings_ru": "<ul><li>учреждение — организация или заведение</li></ul>",
        "definition_ru": "Möbel und Gegenstände, mit denen ein Raum ausgestattet ist.",
        "synonyms": (
            "<ul><li><b>Ausstattung</b> — шире и включает также техническое оснащение.</li>"
            "<li><b>Interieur</b> — чаще подчёркивает оформление внутреннего пространства.</li></ul>"
        ),
        "grammar_table": RICH_NOUN_GRAMMAR,
        "verb_forms": "",
        "etymology": (
            "Существительное образовано от einrichten «обустраивать»; значение развилось "
            "от действия по устройству к результату и месту."
        ),
        "style_and_group": "Нейтральное; жильё и интерьер; также официально об учреждениях.",
        "image_query": "modern room interior",
        "frequency_band": "common",
    }
    data.update(updates)
    return json.dumps(data, ensure_ascii=False)


class FakeResponse:
    def __init__(self, data: object, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> object:
        return self.data

    def raise_for_status(self) -> None:
        return None


class FakePolzaHttpClient:
    def __init__(self, post_response: object, get_responses: list[object] | None = None) -> None:
        self.post_response = post_response
        self.get_responses = get_responses or []
        self.posts: list[tuple[str, dict, float | None]] = []
        self.gets: list[str] = []

    async def post(self, path: str, json: dict, timeout: float | None = None) -> FakeResponse:
        self.posts.append((path, json, timeout))
        return FakeResponse(self.post_response, status_code=201)

    async def get(self, path: str) -> FakeResponse:
        self.gets.append(path)
        return FakeResponse(self.get_responses.pop(0))


def test_chat_json_does_not_set_response_token_limit() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        await client.client.aclose()
        fake_http = FakePolzaHttpClient({"choices": [{"message": {"content": "{}"}}]})
        client.client = fake_http  # type: ignore[assignment]

        content = await client._chat_json([{"role": "user", "content": "test"}])

        assert content == "{}"
        _, payload, _ = fake_http.posts[0]
        assert "max_tokens" not in payload
        assert payload["response_format"] == {"type": "json_object"}

    asyncio.run(run())


def test_card_prompt_and_schema_require_rich_selected_meaning_content() -> None:
    schema = card_json_schema()

    assert "selected meaning only" in schema["meaning_ru"]
    assert "4-6 reliable German synonyms" in schema["synonyms"]
    assert "3-5 short collocations" in schema["grammar_table"]
    assert "Infinitiv / Präteritum / Perfekt" in schema["verb_forms"]
    assert "If origin is uncertain" in schema["etymology"]
    assert "ровно одно полное предложение-пример" in CARD_SYSTEM_PROMPT
    assert "Не смешивай его с другими значениями" in CARD_SYSTEM_PROMPT
    assert "Не используй теги <i>" in CARD_SYSTEM_PROMPT
    assert "verb_forms заполняй только для одиночного немецкого глагола" in CARD_SYSTEM_PROMPT
    for unit in ("существительного", "глагола", "прилагательного", "служебного слова", "фразы/collocation"):
        assert unit in CARD_SYSTEM_PROMPT


def test_korean_cards_use_the_word_instead_of_an_example_sentence() -> None:
    schema = card_json_schema("ko")
    assert "word or phrase only" in schema["sentence_de"]
    assert "without an example sentence" in schema["sentence_de"]

    client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model", language="ko")
    normalized = client._normalize_language_sentence_fields(
        {"word": "학교", "translation_ru": "школа", "sentence_de": "학교에 갑니다.", "sentence_ru": "Я иду в школу."}
    )
    assert normalized["sentence_de"] == "학교"
    assert normalized["sentence_ru"] == "школа"


@pytest.mark.parametrize(
    ("word", "unit_type", "grammar"),
    [
        ("Einrichtung", UnitType.WORD, RICH_NOUN_GRAMMAR),
        (
            "loswerden",
            UnitType.WORD,
            "<ul><li><b>Управление:</b> jemanden oder etwas im Akkusativ loswerden.</li>"
            "<li><b>Формы:</b> wird los, wurde los, ist losgeworden; отделяемый сильный глагол.</li>"
            "<li><b>Коллокации:</b> Sorgen loswerden, Müll loswerden, eine Gewohnheit loswerden.</li>"
            "<li><b>Ошибка:</b> в Perfekt используется sein и форма losgeworden.</li></ul>",
        ),
        (
            "abhängig",
            UnitType.WORD,
            "<ul><li><b>Управление:</b> abhängig von + Dativ.</li>"
            "<li><b>Сравнение:</b> abhängiger, am abhängigsten; обычно сравнение редко нужно.</li>"
            "<li><b>Коллокации:</b> finanziell abhängig, stark abhängig, voneinander abhängig.</li>"
            "<li><b>Ошибка:</b> после von употребляется Dativ, а не Akkusativ.</li></ul>",
        ),
        (
            "obwohl",
            UnitType.WORD,
            "<ul><li><b>Функция:</b> подчинительный союз уступки.</li>"
            "<li><b>Позиция:</b> вводит Nebensatz, спрягаемый глагол стоит в конце.</li>"
            "<li><b>Коллокации:</b> obwohl es regnet, obwohl er weiß, obwohl ich müde bin.</li>"
            "<li><b>Ошибка:</b> не сохранять прямой порядок слов после obwohl.</li></ul>",
        ),
        ("Kunden gewinnen", UnitType.PHRASE, RICH_PHRASE_GRAMMAR),
    ],
)
def test_richness_validation_accepts_part_of_speech_specific_grammar(
    word: str, unit_type: UnitType, grammar: str
) -> None:
    data = json.loads(rich_card_json(word=word, unit_type=unit_type.value, grammar_table=grammar))
    draft = CardDraft(user_id=1, source_text=word, **data)

    validate_generated_draft(draft)


def test_richness_validation_allows_empty_optional_meanings_and_synonyms() -> None:
    data = json.loads(rich_card_json(other_meanings_ru="", synonyms=""))
    draft = CardDraft(user_id=1, source_text="Einrichtung", **data)

    validate_generated_draft(draft)


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"grammar_table": "<ul><li>die Einrichtung</li></ul>"}, "grammar_table"),
        ({"etymology": "ein + richten"}, "etymology"),
        ({"synonyms": "Ausstattung, Interieur"}, "synonyms"),
        ({"other_meanings_ru": "учреждение"}, "other_meanings_ru"),
        ({"style_and_group": "<p>нейтральное</p>"}, "Unsupported HTML tag"),
        ({"style_and_group": "<script>alert(1)</script>"}, "Unsupported HTML tag"),
    ],
)
def test_richness_validation_rejects_shallow_or_invalid_content(
    updates: dict[str, str], error: str
) -> None:
    data = json.loads(rich_card_json(**updates))
    draft = CardDraft(user_id=1, source_text="Einrichtung", **data)

    with pytest.raises(ValueError, match=error):
        validate_generated_draft(draft)


def test_parse_json_object_accepts_object() -> None:
    assert parse_json_object('{"query": "insurance contract"}') == {"query": "insurance contract"}


def test_parse_json_object_accepts_json_encoded_string() -> None:
    assert parse_json_object('"{\\"query\\": \\"insurance contract\\"}"') == {"query": "insurance contract"}


def test_extract_image_url_accepts_openai_like_data() -> None:
    assert extract_image_url({"data": [{"url": "https://example.com/image.png"}]}) == "https://example.com/image.png"


def test_extract_image_url_accepts_top_level_url() -> None:
    assert extract_image_url({"url": "https://example.com/image.png"}) == "https://example.com/image.png"


def test_extract_image_url_accepts_top_level_image_url() -> None:
    assert extract_image_url({"image_url": "https://example.com/image.png"}) == "https://example.com/image.png"


def test_extract_image_url_accepts_nested_json_string() -> None:
    response = {"output": '{"images": [{"url": "https://example.com/image.png"}]}'}

    assert extract_image_url(response) == "https://example.com/image.png"


def test_extract_image_url_rejects_missing_url() -> None:
    with pytest.raises(ValueError, match="missing image URL"):
        extract_image_url({"output": "no image here"})


def test_extract_media_id_accepts_request_id() -> None:
    assert extract_media_id({"requestId": "gen_123"}) == "gen_123"
    assert extract_media_id({"id": "gen_456"}) == "gen_456"


def test_parse_meaning_options_accepts_multiple_options() -> None:
    options = parse_meaning_options(
        {
            "options": [
                {"unit_type": "word", "word": "das Essen", "translation_ru": "еда", "meaning_ru": "прием пищи"},
                {"unit_type": "word", "word": "essen", "translation_ru": "есть", "meaning_ru": "принимать пищу"},
            ]
        },
        fallback_text="Essen",
    )

    assert [option.word for option in options] == ["das Essen", "essen"]
    assert options[0].short_label == "das Essen / еда"


def test_parse_meaning_options_accepts_top_level_list() -> None:
    options = parse_meaning_options(
        [
            {
                "unit_type": "noun",
                "word": "Versicherung",
                "article": "die",
                "translation_ru": "страховка",
                "meaning_ru": "договор страхования",
            }
        ],
        fallback_text="страховка",
    )

    assert len(options) == 1
    assert options[0].word == "Versicherung"
    assert options[0].unit_type == "word"
    assert options[0].short_label == "die Versicherung / страховка"


def test_parse_meaning_selection_accepts_single_wrapped_object_list() -> None:
    selection = parse_meaning_selection(
        [
            {
                "target_source_text": "bewegt",
                "options": [
                    {
                        "unit_type": "adjective",
                        "word": "bewegt",
                        "translation_ru": "растроганный, взволнованный",
                        "meaning_ru": "эмоционально затронутый",
                        "short_label": "эмоции",
                    },
                    {
                        "unit_type": "adjective",
                        "word": "bewegt",
                        "translation_ru": "бурный, насыщенный событиями",
                        "meaning_ru": "полный перемен",
                        "short_label": "о жизни/истории",
                    },
                ],
            }
        ],
        fallback_text="Bewegt",
    )

    assert selection.source_text_for_card == "bewegt"
    assert [option.short_label for option in selection.options] == ["эмоции", "о жизни/истории"]
    assert selection.options[0].translation_ru == "растроганный, взволнованный"


def test_parse_meaning_options_accepts_null_article_and_detects_phrase() -> None:
    options = parse_meaning_options(
        [
            {
                "unit_type": "verb",
                "word": "eine Versicherung abschließen",
                "article": None,
                "translation_ru": "заключить страховку",
                "meaning_ru": "оформить договор страхования",
                "short_label": "оформить страховку",
            }
        ],
        fallback_text="оформить страховку",
    )

    assert len(options) == 1
    assert options[0].article == ""
    assert options[0].unit_type == "phrase"
    assert options[0].word == "eine Versicherung abschließen"


def test_parse_meaning_options_preserves_verb_part_of_speech() -> None:
    options = parse_meaning_options(
        [
            {
                "unit_type": "verb",
                "word": "vereinbaren",
                "article": None,
                "translation_ru": "договариваться",
                "meaning_ru": "согласовать встречу или условия",
            }
        ],
        fallback_text="vereinbaren",
    )

    assert len(options) == 1
    assert options[0].unit_type == UnitType.WORD
    assert options[0].part_of_speech == "verb"
    assert options[0].word == "vereinbaren"


def test_parse_meaning_options_falls_back_for_empty_list() -> None:
    options = parse_meaning_options({"options": []}, fallback_text="Essen")

    assert len(options) == 1
    assert options[0].word == "Essen"


def test_parse_meaning_selection_uses_target_source_text() -> None:
    selection = parse_meaning_selection(
        {
            "target_source_text": "без сдачи",
            "options": [
                {
                    "unit_type": "phrase",
                    "word": "Stimmt so",
                    "translation_ru": "без сдачи",
                    "meaning_ru": "фраза официанту при оплате",
                }
            ],
        },
        fallback_text='Как сказать официанту "без сдачи"',
    )

    assert selection.source_text_for_card == "без сдачи"
    assert selection.options[0].word == "Stimmt so"


def test_parse_meaning_selection_keeps_regular_input_as_fallback() -> None:
    for source_text in ("страховка", "Versicherung", "Сдать экзамен"):
        selection = parse_meaning_selection(
            {"options": [{"unit_type": "word", "word": "Versicherung", "translation_ru": "страховка"}]},
            fallback_text=source_text,
        )

        assert selection.source_text_for_card == source_text


def test_suggest_meanings_prompt_prefers_complete_collocations() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        captured_messages: list[dict[str, object]] = []

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            captured_messages.extend(messages)
            return '{"options":[{"unit_type":"phrase","word":"eine Prüfung bestehen","translation_ru":"сдать экзамен"}]}'

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        options = await client.suggest_meanings("Сдать экзамен")
        await client.close()

        system_prompt = str(captured_messages[0]["content"])
        assert "complete German collocations/phrases" in system_prompt
        assert "Не теряй объект" in system_prompt
        assert "eine Prüfung bestehen" in system_prompt
        assert options[0].word == "eine Prüfung bestehen"

    asyncio.run(run())


def test_suggest_meaning_selection_prompt_extracts_how_to_say_request() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        captured_messages: list[dict[str, object]] = []

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            captured_messages.extend(messages)
            return (
                '{"target_source_text":"без сдачи","options":['
                '{"unit_type":"phrase","word":"Stimmt so","translation_ru":"без сдачи"}'
                "]}"
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        selection = await client.suggest_meaning_selection('Как сказать официанту "без сдачи"')
        await client.close()

        system_prompt = str(captured_messages[0]["content"])
        assert "target_source_text" in system_prompt
        assert "как сказать" in system_prompt
        assert "контекст ситуации" in system_prompt
        assert "Stimmt so" in system_prompt
        assert selection.source_text_for_card == "без сдачи"
        assert selection.options[0].word == "Stimmt so"

    asyncio.run(run())


def test_suggest_meaning_selection_from_audio_prompt_preserves_full_phrase() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        captured_messages: list[dict[str, object]] = []

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            captured_messages.extend(messages)
            return (
                '{"target_source_text":"das Modell","options":['
                '{"unit_type":"phrase","word":"das Modell","translation_ru":"модель",'
                '"explanation_ru":"Полная фраза из аудио."},'
                '{"unit_type":"word","word":"Modell","article":"das","translation_ru":"модель"}'
                "]} "
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        selection = await client.suggest_meaning_selection_from_audio("das Modell")
        await client.close()

        system_prompt = str(captured_messages[0]["content"])
        assert "Это ввод из аудио" in system_prompt
        assert "Первый option обязан быть полной фразой" in system_prompt
        assert "Не сокращай первый option до одного слова" in system_prompt
        assert selection.source_text_for_card == "das Modell"
        assert selection.options[0].unit_type == UnitType.PHRASE
        assert selection.options[0].word == "das Modell"

    asyncio.run(run())


def test_parse_audio_meaning_selection_replaces_single_word_first_option() -> None:
    selection = parse_audio_meaning_selection(
        {
            "target_source_text": "das Modell",
            "options": [
                {"unit_type": "word", "word": "Modell", "article": "das", "translation_ru": "модель"},
                {"unit_type": "word", "word": "Modell", "article": "das", "translation_ru": "модель"},
            ],
        },
        fallback_text="das Modell",
    )

    assert selection.source_text_for_card == "das Modell"
    assert selection.options[0].unit_type == UnitType.PHRASE
    assert selection.options[0].word == "das Modell"
    assert selection.options[0].article == ""


def test_generate_card_repairs_empty_grammar_table() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        responses = [
            rich_card_json(
                unit_type="phrase",
                word="Kunden gewinnen",
                article="",
                gender_color="",
                level="B1",
                translation_ru="привлекать клиентов",
                sentence_de="Wir wollen <b>Kunden gewinnen</b>.",
                sentence_ru="Мы хотим <b>привлекать клиентов</b>.",
                meaning_ru="Заинтересовать новых людей и сделать их клиентами.",
                other_meanings_ru="",
                definition_ru="Neue Menschen als Kunden für ein Angebot gewinnen.",
                synonyms="",
                grammar_table="",
                etymology=(
                    "Фраза состоит из Kunden «клиенты» и gewinnen «приобретать»; переносно глагол "
                    "обозначает привлечение человека на свою сторону."
                ),
                style_and_group="Нейтральное; продажи, маркетинг и деловое общение.",
                image_query="customer acquisition",
            ),
            json.dumps({"grammar_table": RICH_PHRASE_GRAMMAR}, ensure_ascii=False),
        ]

        async def fake_chat_json(messages: list[dict[str, str]]) -> str:
            return responses.pop(0)

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Получить клиента",
            meaning=MeaningOption(word="Kunden gewinnen", translation_ru="получать клиентов"),
        )
        await client.close()

        assert draft.grammar_table == RICH_PHRASE_GRAMMAR
        assert draft.grammar_note_ru == draft.grammar_table

    asyncio.run(run())


def test_generate_card_repairs_missing_verb_forms_for_single_verb() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        responses = [
            rich_card_json(
                unit_type="word",
                word="vereinbaren",
                article="",
                gender_color="",
                level="A2",
                translation_ru="договариваться, согласовывать",
                sentence_de="Wir <b>vereinbaren</b> einen Termin.",
                sentence_ru="Мы <b>договариваемся</b> о встрече.",
                meaning_ru="Приходить к взаимному согласию о времени, условиях или плане.",
                other_meanings_ru="",
                definition_ru="Gemeinsam eine Abmachung treffen.",
                synonyms="<ul><li><b>abmachen</b> — разговорнее, часто о встречах.</li></ul>",
                grammar_table=RICH_VERB_GRAMMAR,
                verb_forms="",
                etymology=(
                    "Глагол образован от verein- и суффикса -baren; исторически связан с идеей "
                    "приведения нескольких сторон к единому соглашению."
                ),
                style_and_group="Нейтральное; встречи, договорённости и деловое общение.",
                image_query="meeting agreement",
            ),
            json.dumps({"verb_forms": "vereinbaren / vereinbarte / hat vereinbart"}, ensure_ascii=False),
        ]

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            return responses.pop(0)

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="vereinbaren",
            meaning=MeaningOption(part_of_speech="verb", word="vereinbaren", translation_ru="договариваться"),
        )
        await client.close()

        assert draft.verb_forms == "vereinbaren / vereinbarte / hat vereinbart"
        assert responses == []

    asyncio.run(run())


def test_generate_card_does_not_require_verb_forms_for_phrase() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        calls = 0

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            nonlocal calls
            calls += 1
            return rich_card_json(
                unit_type="phrase",
                word="eine Prüfung bestehen",
                article="",
                gender_color="",
                level="B1",
                translation_ru="сдать экзамен",
                sentence_de="Sie möchte <b>eine Prüfung bestehen</b>.",
                sentence_ru="Она хочет <b>сдать экзамен</b>.",
                meaning_ru="Успешно пройти экзамен и получить положительный результат.",
                other_meanings_ru="",
                definition_ru="Eine Prüfung erfolgreich abschließen.",
                synonyms="",
                grammar_table=RICH_PHRASE_GRAMMAR,
                verb_forms="",
                etymology=(
                    "Фраза состоит из Prüfung и bestehen; глагол передает успешное прохождение "
                    "испытания или проверки."
                ),
                style_and_group="Нейтральное; учёба, экзамены и официальные проверки.",
                image_query="exam pass",
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="сдать экзамен",
            meaning=MeaningOption(unit_type=UnitType.PHRASE, word="eine Prüfung bestehen", translation_ru="сдать экзамен"),
        )
        await client.close()

        assert draft.verb_forms == ""
        assert calls == 1

    asyncio.run(run())


def test_generate_card_for_meaning_does_not_resend_image(tmp_path) -> None:
    async def run() -> None:
        image_path = tmp_path / "cup.jpg"
        image_path.write_bytes(b"abc")
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        captured_messages: list[dict[str, object]] = []

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            captured_messages.extend(messages)
            return rich_card_json(
                word="Tasse",
                translation_ru="чашка",
                sentence_de="Die <b>Tasse</b> steht auf dem Tisch.",
                sentence_ru="<b>Чашка</b> стоит на столе.",
                meaning_ru="Небольшой сосуд с ручкой, из которого пьют горячие напитки.",
                other_meanings_ru="",
                definition_ru="Ein kleines Gefäß mit Henkel für warme Getränke.",
                grammar_table=RICH_NOUN_GRAMMAR.replace("Einrichtung", "Tasse").replace("Einrichtungen", "Tassen"),
                etymology=(
                    "Слово пришло через французское tasse из арабского; в немецком закрепилось "
                    "как название небольшого сосуда для питья."
                ),
                style_and_group="Нейтральное; посуда, кухня и повседневный быт.",
                image_query="Tasse",
                frequency_band="very_common",
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Фото: моя чашка",
            meaning=MeaningOption(word="Tasse", translation_ru="чашка"),
            image_path=image_path,
            caption="моя чашка",
        )
        await client.close()

        user_content = captured_messages[1]["content"]
        assert isinstance(user_content, str)
        assert "Caption фото: 'моя чашка'" in user_content
        assert "data:image" not in user_content
        assert draft.source_text == "Фото: моя чашка"
        assert draft.word == "Tasse"
        assert draft.grammar_table

    asyncio.run(run())


def test_generate_card_retries_truncated_json_then_succeeds() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        responses = [
            "{",
            rich_card_json(),
        ]

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            return responses.pop(0)

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Фото: Einrichtung",
            meaning=MeaningOption(word="Einrichtung", article="die", translation_ru="обстановка"),
        )
        await client.close()

        assert draft.word == "Einrichtung"
        assert responses == []

    asyncio.run(run())


def test_generate_card_counts_paid_invalid_response_before_retry() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        responses = [
            ("{", {"id": "bad", "usage": {"cost_rub": 0.40}}),
            (rich_card_json(), {"id": "good", "usage": {"cost_rub": 1.05}}),
        ]

        async def fake_chat_json(messages: list[dict[str, object]], cost_tracker=None) -> str:
            content, response = responses.pop(0)
            cost_tracker.record_response(response, event="test")
            return content

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Einrichtung",
            meaning=MeaningOption(word="Einrichtung", translation_ru="обстановка"),
        )
        await client.close()

        assert draft.polza_cost_rub == Decimal("1.45")
        assert draft.polza_cost_complete is True
        assert draft.polza_cost_available is True

    asyncio.run(run())


def test_generate_card_normalizes_safe_html_without_retry_or_extra_cost() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        calls = 0

        async def fake_chat_json(messages: list[dict[str, object]], cost_tracker=None) -> str:
            nonlocal calls
            calls += 1
            cost_tracker.record_response({"id": "one", "usage": {"cost_rub": "1.25"}}, event="test")
            return rich_card_json(
                other_meanings_ru=(
                    "<ul><li><b>гости</b>: <i>Wir erwarten heute Besuch.</i></li></ul>"
                ),
                synonyms=(
                    "<ul><li><strong>Ausstattung</strong> — шире и включает техническое оснащение.</li>"
                    "<li><b>Interieur</b> — подчёркивает оформление пространства.</li></ul>"
                ),
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Besuch",
            meaning=MeaningOption(word="Besuch", article="der", translation_ru="визит"),
        )
        await client.close()

        assert calls == 1
        assert draft.polza_cost_rub == Decimal("1.25")
        assert "<i>" not in draft.other_meanings_ru
        assert "<strong>" not in draft.synonyms
        assert "<b>Ausstattung</b>" in draft.synonyms

    asyncio.run(run())


def test_generate_card_normalizes_br_in_grammar_table_without_retry() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        calls = 0

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            nonlocal calls
            calls += 1
            return rich_card_json(
                grammar_table=RICH_NOUN_GRAMMAR.replace(
                    "Plural die Einrichtungen.",
                    "Plural die Einrichtungen.<br>Употребляется как исчисляемое существительное.",
                )
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Einrichtung",
            meaning=MeaningOption(word="Einrichtung", translation_ru="обстановка"),
        )
        await client.close()

        assert calls == 1
        assert "<br" not in draft.grammar_table.lower()
        assert "Употребляется как исчисляемое существительное" in draft.grammar_table

    asyncio.run(run())


def test_generate_card_retries_polza_timeout_then_succeeds() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        responses: list[object] = [httpx.ReadTimeout("Polza timed out"), rich_card_json()]

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return str(response)

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Einrichtung",
            meaning=MeaningOption(word="Einrichtung", translation_ru="обстановка"),
        )
        await client.close()

        assert draft.word == "Einrichtung"
        assert responses == []

    asyncio.run(run())


def test_generate_card_retries_missing_fields_twice_then_succeeds() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        responses = [
            '{"word":"Einrichtung"}',
            '{"word":"Einrichtung","translation_ru":"обстановка"}',
            rich_card_json(),
        ]

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            return responses.pop(0)

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        draft = await client.generate_card_for_meaning(
            user_id=1,
            source_text="Einrichtung",
            meaning=MeaningOption(word="Einrichtung", translation_ru="обстановка"),
        )
        await client.close()

        assert draft.word == "Einrichtung"
        assert responses == []

    asyncio.run(run())


def test_generate_card_raises_after_three_invalid_responses() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        attempts = 0

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            nonlocal attempts
            attempts += 1
            return "{"

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="трижды вернула неполные данные"):
            await client.generate_card_for_meaning(
                user_id=1,
                source_text="Einrichtung",
                meaning=MeaningOption(word="Einrichtung", translation_ru="обстановка"),
            )
        await client.close()

        assert attempts == 3

    asyncio.run(run())


def test_edit_card_updates_full_draft() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            return rich_card_json(
                word="Versicherung",
                translation_ru="страховка",
                sentence_de="Ich brauche <b>eine Versicherung</b>.",
                sentence_ru="Мне нужна <b>страховка</b>.",
                meaning_ru="Договор финансовой защиты от заранее определённого риска.",
                definition_ru="Ein Vertrag, der vor bestimmten finanziellen Risiken schützt.",
                grammar_table=RICH_NOUN_GRAMMAR.replace("Einrichtung", "Versicherung").replace(
                    "Einrichtungen", "Versicherungen"
                ),
                etymology=(
                    "Образовано от versichern «страховать, заверять» с суффиксом -ung; "
                    "название действия перешло на договор и организацию."
                ),
                style_and_group="Нейтральное; финансы, право и повседневные договоры.",
                image_query="insurance contract",
                frequency_band="medium",
            )

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        updated = await client.edit_card(_draft(), "сделай перевод как страховка")
        await client.close()

        assert updated.translation_ru == "страховка"
        assert "Versicherung" in updated.grammar_table

    asyncio.run(run())


def test_edit_card_adds_polza_cost_to_existing_draft() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")

        async def fake_chat_json(messages: list[dict[str, object]], cost_tracker=None) -> str:
            cost_tracker.record_response(
                {"id": "edit", "usage": {"cost_rub": 0.55}},
                event="edit",
            )
            return rich_card_json()

        client._chat_json = fake_chat_json  # type: ignore[method-assign]
        draft = _draft().model_copy(
            update={
                "polza_cost_rub": Decimal("2.00"),
                "polza_cost_available": True,
            }
        )

        updated = await client.edit_card(draft, "уточни значение")
        await client.close()

        assert updated.polza_cost_rub == Decimal("2.55")
        assert updated.polza_cost_complete is True

    asyncio.run(run())


def test_build_wikimedia_query_variants_accepts_top_level_list() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")

        async def fake_chat_json(messages: list[dict[str, object]]) -> str:
            return '["insurance policy", "insurance contract"]'

        client._chat_json = fake_chat_json  # type: ignore[method-assign]

        queries = await client.build_wikimedia_query_variants(_draft(), ["Versicherung"])
        await client.close()

        assert queries == ["insurance policy", "insurance contract"]

    asyncio.run(run())


def test_image_path_to_data_url_uses_mime_and_base64(tmp_path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"abc")

    assert image_path_to_data_url(image_path) == "data:image/png;base64,YWJj"


def test_audio_path_to_data_url_uses_mime_and_base64(tmp_path) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"abc")

    assert audio_path_to_data_url(audio_path) == "data:audio/ogg;base64,YWJj"


def test_transcribe_audio_posts_audio_payload(tmp_path) -> None:
    async def run() -> None:
        audio_path = tmp_path / "voice.ogg"
        audio_path.write_bytes(b"abc")
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model", "stt-model")
        await client.client.aclose()
        fake_http = FakePolzaHttpClient({"text": "оформить страховку"})
        client.client = fake_http  # type: ignore[assignment]

        text = await client.transcribe_audio(audio_path, user_id=42)

        assert text == "оформить страховку"
        path, payload, timeout = fake_http.posts[0]
        assert path == "/audio/transcriptions"
        assert payload["model"] == "stt-model"
        assert payload["file"] == "data:audio/ogg;base64,YWJj"
        assert payload["response_format"] == "json"
        assert payload["user"] == "42"
        assert timeout is None

    asyncio.run(run())


def test_transcribe_audio_rejects_empty_text(tmp_path) -> None:
    async def run() -> None:
        audio_path = tmp_path / "voice.ogg"
        audio_path.write_bytes(b"abc")
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        await client.client.aclose()
        client.client = FakePolzaHttpClient({"text": "   "})  # type: ignore[assignment]

        with pytest.raises(ValueError, match="empty audio transcription"):
            await client.transcribe_audio(audio_path, user_id=42)

    asyncio.run(run())


def test_generate_image_polls_request_id_and_downloads_completed_url(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        await client.client.aclose()
        fake_http = FakePolzaHttpClient(
            {"requestId": "gen_123"},
            [
                {"id": "gen_123", "status": "pending"},
                {"id": "gen_123", "status": "completed", "data": {"url": "https://example.com/image.png"}},
            ],
        )
        client.client = fake_http  # type: ignore[assignment]

        async def fake_sleep(seconds: float) -> None:
            return None

        async def fake_download(url: str, media_dir: Path, stem: str) -> Path:
            return media_dir / "generated.png"

        monkeypatch.setattr("anki_telegram_bot.polza.asyncio.sleep", fake_sleep)
        monkeypatch.setattr(client, "download_image", fake_download)

        path = await client.generate_image(_draft(), tmp_path)

        assert path == tmp_path / "generated.png"
        assert fake_http.posts[0][0] == "/images/generations"
        assert fake_http.posts[0][2] == 150.0
        assert fake_http.gets == ["/media/gen_123", "/media/gen_123"]

    asyncio.run(run())


def test_wait_for_generated_image_failed_status_raises() -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        await client.client.aclose()
        client.client = FakePolzaHttpClient({}, [{"status": "failed", "error": {"message": "bad prompt"}}])  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="bad prompt"):
            await client.wait_for_generated_image("gen_failed", draft_id=1)

    asyncio.run(run())


def test_wait_for_generated_image_timeout_raises(monkeypatch) -> None:
    async def run() -> None:
        client = PolzaClient("key", "https://polza.ai/api/v1", "text-model", "image-model")
        await client.client.aclose()
        client.client = FakePolzaHttpClient({}, [{"status": "pending"}])  # type: ignore[assignment]

        monkeypatch.setattr("anki_telegram_bot.polza.MEDIA_POLL_MAX_WAIT_SECONDS", 0.01)
        monkeypatch.setattr("anki_telegram_bot.polza.MEDIA_POLL_INTERVAL_SECONDS", 0.01)

        with pytest.raises(TimeoutError, match="timed out"):
            await client.wait_for_generated_image("gen_timeout", draft_id=1)

    asyncio.run(run())


def _draft():
    from anki_telegram_bot.models import CardDraft

    return CardDraft(
        id=1,
        user_id=42,
        source_text="Получить клиента",
        word="Kunden gewinnen",
        word_or_phrase_de="Kunden gewinnen",
        translation_ru="получать клиентов",
        sentence_de="Wir wollen <b>Kunden gewinnen</b>.",
    )
