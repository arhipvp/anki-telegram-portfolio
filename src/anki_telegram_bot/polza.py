import json
import logging
import asyncio
import base64
import mimetypes
import re
import time
from decimal import Decimal
from uuid import uuid4
from pathlib import Path

import httpx

from .costs import PolzaCostTracker
from .logging_config import is_full_debug_enabled, log_event
from .models import CardDraft, ImageEvaluation, MeaningOption, MeaningSelection, UnitType


logger = logging.getLogger(__name__)

IMAGE_GENERATION_TIMEOUT_SECONDS = 150.0
MEDIA_POLL_INTERVAL_SECONDS = 3.0
MEDIA_POLL_MAX_WAIT_SECONDS = 120.0
CARD_GENERATION_MAX_ATTEMPTS = 3
ALLOWED_CARD_HTML_TAGS = frozenset({"b", "ul", "li", "table", "tr", "td"})
STRIP_CARD_HTML_TAGS = frozenset({"i", "em", "span", "font"})
STRUCTURED_CARD_FIELDS = (
    "MeaningRU",
    "OtherMeaningsRU",
    "Synonyms",
    "GrammarTable",
    "Etymology",
    "StyleAndGroup",
)
REQUIRED_CARD_FIELDS = (
    "word",
    "translation_ru",
    "sentence_de",
    "sentence_ru",
    "meaning_ru",
    "definition_ru",
    "level",
)
CARD_HTML_DATA_FIELDS = (
    "sentence_de",
    "sentence_ru",
    "meaning_ru",
    "other_meanings_ru",
    "synonyms",
    "grammar_table",
    "grammar_note_ru",
    "verb_forms",
    "etymology",
    "style_and_group",
)
CARD_HTML_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)([^>]*)>", re.IGNORECASE)


def card_json_schema(language: str = "de") -> dict[str, str]:
    target = {"de": "German", "en": "English", "ko": "Korean"}.get(language, language.title())
    return {
        "unit_type": "word | collocation | phrase",
        "word": f"{target} target word, collocation, or phrase without article",
        "article": "article or empty string",
        "gender_color": "m | f | n | empty string",
        "level": "CEFR level such as A1, A2, B1, B2",
        "translation_ru": "Russian translation",
        "sentence_de": (
            "The target Korean word or phrase only, without an example sentence"
            if language == "ko"
            else f"Simple {target} example, A2-B1, with target highlighted using <b>"
        ),
        "sentence_ru": (
            "Russian translation of the target Korean word or phrase only"
            if language == "ko"
            else "Russian translation of sentence_de, with target translation highlighted using <b>"
        ),
        "meaning_ru": (
            "Precise Russian explanation of the selected meaning only. Do not mix in other meanings."
        ),
        "other_meanings_ru": (
            "Optional <ul><li> list of 2-4 important other meanings with usage labels. "
            "Use empty string when the lexeme has no useful additional meanings."
        ),
        "definition_ru": f"Concise {target} definition of the selected meaning despite legacy field name",
        "synonyms": (
            f"Optional <ul><li> list of up to 4-6 reliable {target} synonyms. Each item must explain "
            "the difference or usage nuance in Russian. Use empty string if there are no true synonyms."
        ),
        "grammar_table": (
            "Required structured HTML <ul> or <table>. Include part-of-speech-specific forms and government, "
            "typical constructions, 3-5 short collocations, and one common mistake when relevant. "
            "Do not add complete example sentences."
        ),
        "verb_forms": (
            "For a single German verb only: plain text Infinitiv / Präteritum / Perfekt with auxiliary, "
            "for example 'vereinbaren / vereinbarte / hat vereinbart'. Empty string for nouns, adjectives, "
            "service words, phrases, and collocations."
            if language == "de"
            else "For a single English verb only: plain text Infinitive / Past Simple / Past Participle. Empty string for nouns, adjectives, service words, phrases, and collocations."
            if language == "en"
            else "For a single Korean verb only: plain text dictionary form / present form / past form. Empty string for nouns, adjectives, service words, phrases, and collocations."
        ),
        "etymology": (
            "Required Russian etymology/morphology note: origin, word formation, and meaning development. "
            "If origin is uncertain, say so explicitly instead of inventing facts."
        ),
        "style_and_group": (
            "Russian usage note covering register, topic, regionality, and restrictions when relevant"
        ),
        "image_query": f"{target} or English image search query, 1-4 words",
        "frequency_band": "very_common | common | medium | rare | special",
    }


CARD_SYSTEM_PROMPT = """
Ты создаёшь содержательные Anki-карточки по немецкому языку для русскоязычного пользователя.
Верни только JSON без markdown. Язык примера должен соответствовать A2-B1, но справочные сведения должны быть точными и полезными.
Для фраз выбирай естественный usable German, а не дословный перевод.
В SentenceDE и SentenceRU выделяй целевое слово или фразу тегом <b>...</b>.
HTML разрешен только для компактных списков <ul><li>...</li></ul>, таблиц <table><tr><td>...</td></tr></table> и выделения <b>.
Не используй теги <i>, <em>, <strong>, <span>, <br> или другие HTML-теги; не добавляй атрибуты к разрешённым тегам.
Создавай ровно одно полное предложение-пример: SentenceDE и его перевод SentenceRU. Не добавляй другие полные предложения в справочные поля.
MeaningRU объясняет только выбранное значение. Не смешивай его с другими значениями слова.
OtherMeaningsRU: до 2-4 действительно важных других значений с пометами употребления в <ul><li>; если их нет, верни пустую строку.
DefinitionRU, несмотря на legacy-имя, содержит короткое определение выбранного значения на немецком.
Synonyms: до 4-6 надёжных немецких синонимов в <ul><li>; после каждого дай по-русски краткое отличие или оттенок. Если настоящих синонимов нет, верни пустую строку.
grammar_table обязателен и должен быть содержательным. Дай формы и управление, характерные для части речи, типичные конструкции, 3-5 коротких коллокаций и одну частую ошибку, если она релевантна.
Для существительного укажи род, Genitiv и Plural; для глагола — управление, Präsens, Präteritum, Perfekt, отделяемость и возвратность; для прилагательного — сравнение и типичное управление; для служебного слова — функцию, позицию и порядок слов; для фразы/collocation — структуру, управление и вариативные части.
verb_forms заполняй только для одиночного немецкого глагола: Infinitiv / Präteritum / Perfekt с haben/sein, например vereinbaren / vereinbarte / hat vereinbart. Для существительных, прилагательных, служебных слов, фраз и collocation верни пустую строку.
Etymology обязателен: по-русски объясни происхождение, состав слова и развитие значения. Если достоверная этимология неясна, прямо укажи неопределённость.
StyleAndGroup по-русски описывает регистр, тематику, региональность и ограничения употребления, когда они есть.
"""

ENGLISH_CARD_SYSTEM_PROMPT = """
Ты создаёшь содержательные Anki-карточки по английскому языку для русскоязычного пользователя.
Верни только JSON без markdown. Язык примера должен соответствовать A2-B1, но справочные сведения должны быть точными и полезными.
Для фраз выбирай естественный usable English, а не дословный перевод.
В SentenceDE и SentenceRU выделяй целевое слово или фразу тегом <b>...</b>; SentenceDE содержит английский текст.
HTML разрешен только для компактных списков <ul><li>...</li></ul>, таблиц <table><tr><td>...</td></tr></table> и выделения <b>.
Не используй теги <i>, <em>, <strong>, <span>, <br> или другие HTML-теги; не добавляй атрибуты к разрешённым тегам.
Создавай ровно одно полное предложение-пример: SentenceDE и его перевод SentenceRU. Не добавляй другие полные предложения в справочные поля.
MeaningRU объясняет только выбранное значение. Не смешивай его с другими значениями слова.
OtherMeaningsRU: до 2-4 действительно важных других значений с пометами употребления в <ul><li>; если их нет, верни пустую строку.
DefinitionRU, несмотря на legacy-имя, содержит короткое определение выбранного значения на английском.
Synonyms: до 4-6 надёжных английских синонимов в <ul><li>; после каждого дай по-русски краткое отличие или оттенок. Если настоящих синонимов нет, верни пустую строку.
grammar_table обязателен и должен быть содержательным. Дай формы и управление по части речи, типичные конструкции, 3-5 коротких коллокаций и одну частую ошибку, если она релевантна.
verb_forms заполняй только для одиночного английского глагола: Infinitive / Past Simple / Past Participle. Для остальных частей речи, фраз и collocation верни пустую строку.
Etymology обязателен: по-русски объясни происхождение, состав слова и развитие значения. Если достоверная этимология неясна, прямо укажи неопределённость.
StyleAndGroup по-русски описывает регистр, тематику, региональность и ограничения употребления, когда они есть.
"""

KOREAN_CARD_SYSTEM_PROMPT = """
Ты создаёшь содержательные Anki-карточки по корейскому языку для русскоязычного пользователя.
Верни только JSON без markdown. В SentenceDE храни только корейское целевое слово или выражение, без предложения-примера. В SentenceRU храни только его русский перевод, также без предложения.
Не добавляй точку, пояснение или дополнительный текст в SentenceDE и SentenceRU. Полезные примеры и конструкции помещай только в GrammarTable.
DefinitionRU содержит короткое определение выбранного значения на корейском, остальные объяснения и грамматика — по-русски.
Synonyms заполняй корейскими синонимами с русским объяснением оттенков. GrammarTable должен включать частицу/окончание, формы вежливости, типичные конструкции, 3-5 коротких коллокаций и частую ошибку.
Для глагола verb_forms укажи словарную форму, настоящее и прошедшее время; для остальных частей речи верни пустую строку.
Etymology и StyleAndGroup обязательны и пишутся по-русски. Используй только разрешённые HTML-теги <b>, <ul>, <li>, <table>, <tr>, <td>.
"""


class PolzaClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        text_model: str,
        image_model: str,
        transcription_model: str = "openai/gpt-4o-mini-transcribe",
        language: str = "de",
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.text_model = text_model
        self.image_model = image_model
        self.transcription_model = transcription_model
        self.language = language
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    async def close(self) -> None:
        await self.client.aclose()
        log_event(logger, logging.INFO, "polza.client.closed", base_url=self.base_url)

    @property
    def language_name(self) -> str:
        return {"de": "немецкие", "en": "английские", "ko": "корейские"}.get(self.language, self.language)

    @property
    def card_system_prompt(self) -> str:
        return {"de": CARD_SYSTEM_PROMPT, "en": ENGLISH_CARD_SYSTEM_PROMPT, "ko": KOREAN_CARD_SYSTEM_PROMPT}.get(self.language, CARD_SYSTEM_PROMPT)

    def _normalize_language_sentence_fields(self, data: dict) -> dict:
        if self.language == "ko":
            data["sentence_de"] = str(data.get("word") or "").strip()
            data["sentence_ru"] = str(data.get("translation_ru") or "").strip()
        return data

    async def generate_card(self, user_id: int, source_text: str) -> CardDraft:
        selection = await self.suggest_meaning_selection(source_text)
        return await self.generate_card_for_meaning(
            user_id=user_id,
            source_text=selection.source_text_for_card,
            meaning=selection.options[0],
            polza_cost_rub=selection.polza_cost_rub,
            polza_cost_complete=selection.polza_cost_complete,
            polza_cost_available=selection.polza_cost_available,
        )

    async def transcribe_audio(self, path: Path, user_id: int | None = None) -> str:
        started_at = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "polza.audio_transcription.start",
            path=path,
            user_id=user_id,
            model=self.transcription_model,
            size_bytes=path.stat().st_size if path.exists() else None,
        )
        request_json = {
            "model": self.transcription_model,
            "file": audio_path_to_data_url(path),
            "response_format": "json",
            "temperature": 0,
            "user": str(user_id) if user_id is not None else "",
        }
        response = await self.client.post("/audio/transcriptions", json=request_json)
        response.raise_for_status()
        response_json = response.json()
        if isinstance(response_json, str):
            text = response_json
        elif isinstance(response_json, dict):
            text = str(response_json.get("text") or "")
        else:
            text = ""
        text = text.strip()
        if not text:
            log_event(
                logger,
                logging.ERROR,
                "polza.audio_transcription.empty",
                path=path,
                response_json=response_json if is_full_debug_enabled() else None,
            )
            raise ValueError("Polza returned empty audio transcription")
        log_event(
            logger,
            logging.INFO,
            "polza.audio_transcription.success",
            path=path,
            user_id=user_id,
            model=self.transcription_model,
            duration_ms=_duration_ms(started_at),
            text=text if is_full_debug_enabled() else None,
        )
        return text

    async def suggest_meanings(self, source_text: str) -> list[MeaningOption]:
        return (await self.suggest_meaning_selection(source_text)).options

    async def suggest_meaning_selection(self, source_text: str) -> MeaningSelection:
        started_at = time.perf_counter()
        cost_tracker = PolzaCostTracker()
        log_event(logger, logging.INFO, "polza.meanings.start", source_text=source_text)
        language_context = (
            'для карточки, а контекст ситуации используй для естественных немецких вариантов. '
            'Например для \'Как сказать официанту "без сдачи"\' target_source_text=\'без сдачи\', '
            "а варианты: 'Stimmt so', 'Das stimmt so', 'Der Rest ist für Sie', не дословное 'ohne Wechselgeld'. "
            "Если исходный ввод состоит из нескольких слов или содержит объект действия, сначала предлагай "
            "естественные complete German collocations/phrases, а не голый глагол. "
            "Не теряй объект: для 'Сдать экзамен' предлагай 'eine Prüfung bestehen' или "
            "'eine Prüfung ablegen', а не только 'bestehen'."
            if self.language == "de"
            else f"для карточки, а контекст ситуации используй для естественных вариантов на {self.language_name}. "
            f"Предлагай complete {self.language_name} collocations/phrases, а не голый глагол. Не теряй объект действия."
        )
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        "Верни только JSON: target_source_text string, options array, 1-5 вариантов. "
                        "Каждый вариант: unit_type, part_of_speech, word, article, translation_ru, meaning_ru, short_label, explanation_ru. "
                        "Если вариант — одиночный глагол, unit_type='verb' и part_of_speech='verb'; для Anki он будет сохранён как word. "
                        "target_source_text — текст, который будет сохранен как исходный смысл карточки. "
                        "Если пользователь отправил обычное слово или фразу, target_source_text равен исходному вводу. "
                        "Если пользователь спрашивает, как сказать что-то в ситуации, извлеки именно русскую фразу/смысл "
                        f"{language_context} "
                        "Например для 'Как сказать официанту \"без сдачи\"' target_source_text='без сдачи', "
                        ""
                        "Разделяй разные части речи и значения, например Essen как das Essen / еда и essen / есть. "
                        ""
                    ),
                },
                {"role": "user", "content": f"Найди возможные значения для карточки: {source_text!r}"},
            ],
            cost_tracker=cost_tracker,
        )
        data = parse_json_value(content)
        selection = parse_meaning_selection(data, fallback_text=source_text).model_copy(
            update={
                "polza_cost_rub": cost_tracker.amount_rub,
                "polza_cost_complete": cost_tracker.complete,
                "polza_cost_available": cost_tracker.available,
            }
        )
        log_event(
            logger,
            logging.INFO,
            "polza.meanings.success",
            source_text=source_text,
            source_text_for_card=selection.source_text_for_card,
            option_count=len(selection.options),
            options=selection.options if is_full_debug_enabled() else None,
            duration_ms=_duration_ms(started_at),
        )
        return selection

    async def suggest_meaning_selection_from_audio(self, transcript: str) -> MeaningSelection:
        started_at = time.perf_counter()
        cost_tracker = PolzaCostTracker()
        log_event(logger, logging.INFO, "polza.audio_meanings.start", transcript=transcript)
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        "Верни только JSON: target_source_text string, options array, 1-5 вариантов. "
                        "Это ввод из аудио. Главная задача — сделать карточку из полной распознанной фразы, "
                        "которая звучит в аудио. Первый option обязан быть полной фразой или collocation из transcript. "
                        "Не сокращай первый option до одного слова, если transcript содержит несколько слов. "
                        "Исправь только очевидные ошибки регистра, артикля или пунктуации, но не придумывай другую фразу. "
                        "target_source_text должен быть этой полной фразой. "
                        "Каждый вариант: unit_type, part_of_speech, word, article, translation_ru, meaning_ru, short_label, explanation_ru. "
                        "Если вариант — одиночный глагол, unit_type='verb' и part_of_speech='verb'; для Anki он будет сохранён как word. "
                        "Для первого варианта unit_type должен быть phrase или collocation, если transcript содержит несколько слов. "
                        "Остальные варианты могут быть полезными подфразами или ключевыми словами только как вторичные варианты. "
                        "В explanation_ru для первого варианта явно укажи, что это полная фраза из аудио."
                    ),
                },
                {"role": "user", "content": f"Подготовь варианты для карточки по аудио transcript: {transcript!r}"},
            ],
            cost_tracker=cost_tracker,
        )
        selection = parse_audio_meaning_selection(parse_json_value(content), fallback_text=transcript).model_copy(
            update={
                "polza_cost_rub": cost_tracker.amount_rub,
                "polza_cost_complete": cost_tracker.complete,
                "polza_cost_available": cost_tracker.available,
            }
        )
        log_event(
            logger,
            logging.INFO,
            "polza.audio_meanings.success",
            transcript=transcript,
            source_text_for_card=selection.source_text_for_card,
            option_count=len(selection.options),
            options=selection.options if is_full_debug_enabled() else None,
            duration_ms=_duration_ms(started_at),
        )
        return selection

    async def suggest_meanings_from_image(self, image_path: Path, caption: str = "") -> list[MeaningOption]:
        return (await self.suggest_meaning_selection_from_image(image_path, caption)).options

    async def suggest_meaning_selection_from_image(
        self,
        image_path: Path,
        caption: str = "",
    ) -> MeaningSelection:
        started_at = time.perf_counter()
        cost_tracker = PolzaCostTracker()
        source_text = f"Фото: {caption}".strip() if caption else "Фото"
        log_event(
            logger,
            logging.INFO,
            "polza.image_meanings.start",
            image_path=image_path,
            caption=caption if is_full_debug_enabled() else None,
            size_bytes=image_path.stat().st_size if image_path.exists() else None,
        )
        data_url = image_path_to_data_url(image_path)
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        "Верни только JSON: options array, 1-5 вариантов. "
                        "Каждый вариант: unit_type, part_of_speech, word, article, translation_ru, meaning_ru, short_label, explanation_ru. "
                        "Если вариант — одиночный глагол, unit_type='verb' и part_of_speech='verb'; для Anki он будет сохранён как word. "
                        f"По фото выбери возможные {self.language_name} слова, collocations или phrases для главных объектов/действий."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Определи возможные значения для Anki-карточки по фото. Caption: {caption!r}"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            cost_tracker=cost_tracker,
        )
        options = parse_meaning_options(parse_json_value(content), fallback_text=source_text)
        log_event(
            logger,
            logging.INFO,
            "polza.image_meanings.success",
            image_path=image_path,
            option_count=len(options),
            options=options if is_full_debug_enabled() else None,
            duration_ms=_duration_ms(started_at),
        )
        return MeaningSelection(
            source_text_for_card=source_text,
            options=options,
            polza_cost_rub=cost_tracker.amount_rub,
            polza_cost_complete=cost_tracker.complete,
            polza_cost_available=cost_tracker.available,
        )

    async def generate_card_for_meaning(
        self,
        user_id: int,
        source_text: str,
        meaning: MeaningOption,
        image_path: Path | None = None,
        caption: str = "",
        polza_cost_rub: Decimal = Decimal("0"),
        polza_cost_complete: bool = True,
        polza_cost_available: bool = False,
    ) -> CardDraft:
        started_at = time.perf_counter()
        cost_tracker = PolzaCostTracker(
            amount_rub=polza_cost_rub,
            complete=polza_cost_complete,
            available=polza_cost_available,
        )
        log_event(
            logger,
            logging.INFO,
            "polza.generate_card.start",
            user_id=user_id,
            source_text=source_text,
            meaning=meaning if is_full_debug_enabled() else meaning.short_label,
            image_path=image_path,
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": self.card_system_prompt},
            {
                "role": "user",
                "content": (
                    "Создай карточку строго для выбранного значения. "
                    f"Исходный ввод: {source_text!r}. "
                    f"Выбранное значение: {meaning.model_dump_json()}. "
                    f"Caption фото: {caption!r}. "
                    f"JSON schema: {json.dumps(card_json_schema(self.language), ensure_ascii=False)}"
                ),
            },
        ]
        draft = await self._generate_card_with_retries(
            messages=messages,
            user_id=user_id,
            source_text=source_text,
            cost_tracker=cost_tracker,
            meaning=meaning,
        )
        log_event(
            logger,
            logging.INFO,
            "polza.generate_card.success",
            user_id=user_id,
            duration_ms=_duration_ms(started_at),
            draft=draft if is_full_debug_enabled() else None,
        )
        return draft

    async def _generate_card_with_retries(
        self,
        messages: list[dict[str, object]],
        user_id: int,
        source_text: str,
        cost_tracker: PolzaCostTracker,
        meaning: MeaningOption | None = None,
    ) -> CardDraft:
        last_error: Exception | None = None
        for attempt in range(1, CARD_GENERATION_MAX_ATTEMPTS + 1):
            try:
                content = await self._chat_json_with_cost(messages, cost_tracker=cost_tracker)
                data = parse_json_object(content)
                validate_card_data(data)
                data = self._normalize_language_sentence_fields(data)
                data["word_or_phrase_de"] = data["word"]
                data["example_de"] = data["sentence_de"]
                data["example_ru"] = data["sentence_ru"]
                data = await self.ensure_grammar_table(data, source_text, cost_tracker=cost_tracker)
                data = await self.ensure_verb_forms(data, source_text, meaning, cost_tracker=cost_tracker)
                normalize_card_html_data(data)
                draft = CardDraft(
                    user_id=user_id,
                    source_text=source_text,
                    polza_cost_rub=cost_tracker.amount_rub,
                    polza_cost_complete=cost_tracker.complete,
                    polza_cost_available=cost_tracker.available,
                    **data,
                )
                validate_generated_draft(draft)
                return draft
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as exc:
                last_error = exc
                log_event(
                    logger,
                    logging.WARNING,
                    "polza.card_generation.retry",
                    source_text=source_text,
                    attempt=attempt,
                    max_attempts=CARD_GENERATION_MAX_ATTEMPTS,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if attempt == CARD_GENERATION_MAX_ATTEMPTS:
                    break

        raise RuntimeError(
            "Polza трижды вернула неполные данные. Нажми выбранное значение ещё раз."
        ) from last_error

    async def generate_card_from_image(
        self,
        user_id: int,
        image_path: Path,
        caption: str = "",
    ) -> CardDraft:
        source_text = f"Фото: {caption}".strip() if caption else "Фото"
        selection = await self.suggest_meaning_selection_from_image(image_path, caption)
        return await self.generate_card_for_meaning(
            user_id,
            source_text,
            selection.options[0],
            image_path=image_path,
            caption=caption,
            polza_cost_rub=selection.polza_cost_rub,
            polza_cost_complete=selection.polza_cost_complete,
            polza_cost_available=selection.polza_cost_available,
        )

    async def edit_card(self, draft: CardDraft, instruction: str) -> CardDraft:
        started_at = time.perf_counter()
        cost_tracker = tracker_from_draft(draft)
        log_event(logger, logging.INFO, "polza.edit_card.start", draft_id=draft.id, instruction=instruction)
        content = await self._chat_json_with_cost(
            [
                {"role": "system", "content": self.card_system_prompt + "\nВерни полный JSON карточки со всеми полями схемы."},
                {
                    "role": "user",
                    "content": (
                        "Исправь карточку по инструкции пользователя. "
                        f"Инструкция: {instruction!r}. "
                        f"Текущая карточка: {draft.model_dump_json()}. "
                        f"JSON schema: {json.dumps(card_json_schema(self.language), ensure_ascii=False)}"
                    ),
                },
            ],
            cost_tracker=cost_tracker,
        )
        data = parse_json_object(content)
        data["word_or_phrase_de"] = data.get("word", draft.word_or_phrase_de)
        data["example_de"] = data.get("sentence_de", draft.example_de)
        data["example_ru"] = data.get("sentence_ru", draft.example_ru)
        data = self._normalize_language_sentence_fields({**draft.model_dump(), **data})
        data["word_or_phrase_de"] = data.get("word", draft.word_or_phrase_de)
        data["example_de"] = data.get("sentence_de", draft.example_de)
        data["example_ru"] = data.get("sentence_ru", draft.example_ru)
        data = await self.ensure_grammar_table(
            {**draft.model_dump(), **data},
            draft.source_text,
            cost_tracker=cost_tracker,
        )
        data = await self.ensure_verb_forms(data, draft.source_text, None, cost_tracker=cost_tracker)
        data.update(cost_fields(cost_tracker))
        updated = CardDraft.model_validate({**draft.model_dump(mode="json"), **data})
        log_event(
            logger,
            logging.INFO,
            "polza.edit_card.success",
            draft_id=draft.id,
            duration_ms=_duration_ms(started_at),
            draft=updated if is_full_debug_enabled() else None,
        )
        return updated

    async def regenerate_example(self, draft: CardDraft, instruction: str | None = None) -> CardDraft:
        started_at = time.perf_counter()
        cost_tracker = tracker_from_draft(draft)
        log_event(logger, logging.INFO, "polza.regenerate_example.start", draft_id=draft.id, instruction=instruction)
        if self.language == "ko":
            word = draft.word or draft.word_or_phrase_de
            updated = draft.model_copy(
                update={
                    "sentence_de": word,
                    "sentence_ru": draft.translation_ru,
                    "example_de": word,
                    "example_ru": draft.translation_ru,
                }
            )
            log_event(logger, logging.INFO, "polza.regenerate_example.success", draft_id=draft.id, duration_ms=_duration_ms(started_at), draft=updated if is_full_debug_enabled() else None)
            return updated
        content = await self._chat_json_with_cost(
            [
                {"role": "system", "content": "Верни только JSON: sentence_de, sentence_ru. Выдели целевое слово или фразу через <b>...</b>."},
                {
                    "role": "user",
                    "content": (
                        f"Сделай другой простой пример A2-B1 для {draft.word or draft.word_or_phrase_de} "
                        f"({draft.translation_ru}). Инструкция: {instruction or 'обычный бытовой пример'}"
                    ),
                },
            ],
            cost_tracker=cost_tracker,
        )
        data = parse_json_object(content)
        updated = draft.model_copy(
            update={
                "sentence_de": data["sentence_de"],
                "sentence_ru": data["sentence_ru"],
                "example_de": data["sentence_de"],
                "example_ru": data["sentence_ru"],
                **cost_fields(cost_tracker),
            }
        )
        log_event(logger, logging.INFO, "polza.regenerate_example.success", draft_id=draft.id, duration_ms=_duration_ms(started_at), draft=updated if is_full_debug_enabled() else None)
        return updated

    async def simplify(self, draft: CardDraft) -> CardDraft:
        started_at = time.perf_counter()
        cost_tracker = tracker_from_draft(draft)
        log_event(logger, logging.INFO, "polza.simplify.start", draft_id=draft.id, draft=draft if is_full_debug_enabled() else None)
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        "Верни только JSON с полями word, article, gender_color, level, translation_ru, "
                        "sentence_de, sentence_ru, meaning_ru, definition_ru, synonyms, grammar_table, verb_forms, style_and_group, image_query."
                    ),
                },
                {"role": "user", "content": f"Упрости карточку для уровня A2-B1: {draft.model_dump_json()}"},
            ],
            cost_tracker=cost_tracker,
        )
        data = parse_json_object(content)
        data["word_or_phrase_de"] = data.get("word", draft.word_or_phrase_de)
        data["example_de"] = data.get("sentence_de", draft.example_de)
        data["example_ru"] = data.get("sentence_ru", draft.example_ru)
        data = self._normalize_language_sentence_fields({**draft.model_dump(), **data})
        data["word_or_phrase_de"] = data.get("word", draft.word_or_phrase_de)
        data["example_de"] = data.get("sentence_de", draft.example_de)
        data["example_ru"] = data.get("sentence_ru", draft.example_ru)
        data = await self.ensure_grammar_table(
            {**draft.model_dump(), **data},
            draft.source_text,
            cost_tracker=cost_tracker,
        )
        data = await self.ensure_verb_forms(data, draft.source_text, None, cost_tracker=cost_tracker)
        data.update(cost_fields(cost_tracker))
        updated = draft.model_copy(update=data)
        log_event(logger, logging.INFO, "polza.simplify.success", draft_id=draft.id, duration_ms=_duration_ms(started_at), draft=updated if is_full_debug_enabled() else None)
        return updated

    async def ensure_grammar_table(
        self,
        data: dict,
        source_text: str,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> dict:
        grammar_table = str(data.get("grammar_table") or "").strip()
        if grammar_table:
            data["grammar_table"] = grammar_table
            data["grammar_note_ru"] = grammar_table
            return data

        started_at = time.perf_counter()
        log_event(
            logger,
            logging.WARNING,
            "polza.grammar_table.missing",
            source_text=source_text,
            card_data=data if is_full_debug_enabled() else None,
        )
        grammar_table = await self.build_grammar_note(data, source_text, cost_tracker=cost_tracker)
        data["grammar_table"] = grammar_table
        data["grammar_note_ru"] = grammar_table
        log_event(
            logger,
            logging.INFO,
            "polza.grammar_table.repaired",
            source_text=source_text,
            duration_ms=_duration_ms(started_at),
            grammar_table=grammar_table if is_full_debug_enabled() else None,
        )
        return data

    async def ensure_verb_forms(
        self,
        data: dict,
        source_text: str,
        meaning: MeaningOption | None,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> dict:
        verb_forms = str(data.get("verb_forms") or "").strip()
        if verb_forms:
            data["verb_forms"] = validate_verb_forms_text(verb_forms)
            return data
        if not needs_verb_forms(data, meaning):
            data["verb_forms"] = ""
            return data

        started_at = time.perf_counter()
        log_event(
            logger,
            logging.WARNING,
            "polza.verb_forms.missing",
            source_text=source_text,
            word=data.get("word"),
        )
        verb_forms = await self.build_verb_forms(data, source_text, cost_tracker=cost_tracker)
        data["verb_forms"] = verb_forms
        log_event(
            logger,
            logging.INFO,
            "polza.verb_forms.repaired",
            source_text=source_text,
            duration_ms=_duration_ms(started_at),
            verb_forms=verb_forms,
        )
        return data

    async def build_verb_forms(
        self,
        data: dict,
        source_text: str,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> str:
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        f"Верни только JSON: verb_forms для {self.language_name} глагола. Простая строка без HTML. "
                        "Для немецкого: Infinitiv / Präteritum / Perfekt с haben/sein; для английского: Infinitive / Past Simple / Past Participle."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Заполни verb_forms для одиночного глагола на {self.language_name}. Исходный ввод: {source_text!r}. "
                        f"Карточка: {json.dumps(data, ensure_ascii=False, default=str)}"
                    ),
                },
            ],
            cost_tracker=cost_tracker,
        )
        verb_forms = str(parse_json_object(content).get("verb_forms") or "").strip()
        if not verb_forms:
            log_event(
                logger,
                logging.ERROR,
                "polza.verb_forms.repair_empty",
                source_text=source_text,
                content=content if is_full_debug_enabled() else None,
            )
            raise ValueError("Polza returned empty verb_forms after repair")
        return validate_verb_forms_text(verb_forms)

    async def build_grammar_note(
        self,
        data: dict,
        source_text: str,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> str:
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        "Верни только JSON: grammar_table. Поле обязательно и не должно быть пустым. "
                        "Используй HTML <ul><li>...</li></ul> или <table><tr><td>...</td></tr></table>, минимум 4 пункта. "
                        "Укажи формы и управление по части речи, типичные конструкции, 3-5 коротких коллокаций "
                        "и частую ошибку, если она релевантна. Для фразы/collocation опиши структуру, управление, "
                        "порядок слов и вариативные части. Не добавляй полные предложения-примеры."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Заполни grammar_table для карточки на {self.language_name}. Исходный ввод: {source_text!r}. "
                        f"Карточка: {json.dumps(data, ensure_ascii=False, default=str)}"
                    ),
                },
            ],
            cost_tracker=cost_tracker,
        )
        grammar_table = str(parse_json_object(content).get("grammar_table") or "").strip()
        if not grammar_table:
            log_event(
                logger,
                logging.ERROR,
                "polza.grammar_table.repair_empty",
                source_text=source_text,
                content=content if is_full_debug_enabled() else None,
            )
            raise ValueError("Polza returned empty grammar_table after repair")
        return grammar_table

    async def build_wikimedia_query(
        self,
        draft: CardDraft,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> str:
        if draft.image_query:
            log_event(logger, logging.INFO, "polza.wikimedia_query.from_draft", draft_id=draft.id, query=draft.image_query)
            return draft.image_query
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "polza.wikimedia_query.start", draft_id=draft.id, draft=draft if is_full_debug_enabled() else None)
        content = await self._chat_json_with_cost(
            [
                {"role": "system", "content": "Верни только JSON: query. Query должен быть на английском или немецком, 1-4 слова."},
                {"role": "user", "content": f"Сформируй Wikimedia image search query для карточки: {draft.model_dump_json()}"},
            ],
            cost_tracker=cost_tracker,
        )
        query = parse_json_object(content)["query"]
        log_event(logger, logging.INFO, "polza.wikimedia_query.success", draft_id=draft.id, query=query, duration_ms=_duration_ms(started_at))
        return query

    async def build_wikimedia_query_variants(
        self,
        draft: CardDraft,
        failed_queries: list[str],
        cost_tracker: PolzaCostTracker | None = None,
    ) -> list[str]:
        started_at = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "polza.wikimedia_query_variants.start",
            draft_id=draft.id,
            failed_queries=failed_queries,
            draft=draft if is_full_debug_enabled() else None,
        )
        content = await self._chat_json_with_cost(
            [
                {
                    "role": "system",
                    "content": (
                        "Верни только JSON: queries array of 2-4 strings. "
                        "Каждый query для Wikimedia image search, на английском или немецком, 1-4 слова. "
                        "Избегай уже неудачных query и слов вроде text, page, article."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Нужны альтернативные поисковые запросы для картинки без текста. "
                        f"Карточка: {draft.model_dump_json()}. Неудачные query: {failed_queries}"
                    ),
                },
            ],
            cost_tracker=cost_tracker,
        )
        data = parse_json_value(content)
        raw_queries = data.get("queries", []) if isinstance(data, dict) else data
        if not isinstance(raw_queries, list):
            raw_queries = []
        queries = [str(query).strip() for query in raw_queries if str(query).strip()]
        log_event(
            logger,
            logging.INFO,
            "polza.wikimedia_query_variants.success",
            draft_id=draft.id,
            queries=queries,
            duration_ms=_duration_ms(started_at),
        )
        return queries

    async def evaluate_image(
        self,
        draft: CardDraft,
        image_url: str,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> ImageEvaluation:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "polza.evaluate_image.start", draft_id=draft.id, image_url=image_url)
        prompt = (
            "Оцени, подходит ли изображение для Anki-карточки. "
            "Картинка должна визуально помогать вспомнить смысл и не содержать текст-подсказку. "
            f"Карточка: {draft.word or draft.word_or_phrase_de} / {draft.translation_ru}. Image URL: {image_url}"
        )
        content = await self._chat_json_with_cost(
            [
                {"role": "system", "content": "Верни только JSON: is_match boolean, reason string."},
                {"role": "user", "content": prompt},
            ],
            cost_tracker=cost_tracker,
        )
        evaluation = ImageEvaluation.model_validate(parse_json_object(content))
        log_event(
            logger,
            logging.INFO,
            "polza.evaluate_image.success",
            draft_id=draft.id,
            image_url=image_url,
            is_match=evaluation.is_match,
            reason=evaluation.reason,
            duration_ms=_duration_ms(started_at),
        )
        return evaluation

    async def generate_image(
        self,
        draft: CardDraft,
        media_dir: Path,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> Path:
        started_at = time.perf_counter()
        prompt = (
            f"Create a simple realistic image for an {self.language_name} learning flashcard. "
            "No text, no letters, no labels, no watermark. "
            f"Concept: {draft.word or draft.word_or_phrase_de} / {draft.translation_ru}. "
            f"Example context: {draft.sentence_de or draft.example_de}"
        )
        request_json = {
            "model": self.image_model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "quality": "standard",
            "response_format": "url",
        }
        log_event(
            logger,
            logging.INFO,
            "polza.image_generation.request",
            draft_id=draft.id,
            endpoint="/images/generations",
            request_json=request_json if is_full_debug_enabled() else {"model": self.image_model, "size": "1024x1024"},
        )
        response = await self.client.post(
            "/images/generations",
            json=request_json,
            timeout=IMAGE_GENERATION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        response_json = response.json()
        log_event(
            logger,
            logging.INFO,
            "polza.image_generation.response",
            draft_id=draft.id,
            status_code=response.status_code,
            headers=dict(response.headers),
            duration_ms=_duration_ms(started_at),
            response_json=response_json if is_full_debug_enabled() else None,
        )
        image_url = extract_image_url_or_none(response_json)
        if image_url is None:
            media_id = extract_media_id(response_json)
            if media_id:
                if cost_tracker:
                    cost_tracker.record_response(
                        response_json,
                        event="image_generation.start",
                        charge_id=media_id,
                        missing_cost_is_incomplete=False,
                    )
                image_url = await self.wait_for_generated_image(
                    media_id,
                    draft.id,
                    cost_tracker=cost_tracker,
                )
            else:
                if cost_tracker:
                    cost_tracker.record_response(response_json, event="image_generation.sync")
                error = ValueError("Polza image generation response missing image URL or media id")
                log_event(
                    logger,
                    logging.ERROR,
                    "polza.image_generation.invalid_response",
                    draft_id=draft.id,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    response_json=response_json if is_full_debug_enabled() else None,
                    error=error,
                )
                raise error
        elif cost_tracker:
            cost_tracker.record_response(response_json, event="image_generation.sync")
        return await self.download_image(image_url, media_dir, f"polza_{draft.user_id}_{uuid4().hex}")

    async def wait_for_generated_image(
        self,
        media_id: str,
        draft_id: int | None,
        cost_tracker: PolzaCostTracker | None = None,
    ) -> str:
        started_at = time.perf_counter()
        max_attempts = max(1, int(MEDIA_POLL_MAX_WAIT_SECONDS / MEDIA_POLL_INTERVAL_SECONDS))
        for attempt in range(1, max_attempts + 1):
            response = await self.client.get(f"/media/{media_id}")
            response.raise_for_status()
            response_json = response.json()
            status = str(response_json.get("status") or "").casefold() if isinstance(response_json, dict) else ""
            elapsed_ms = _duration_ms(started_at)
            log_event(
                logger,
                logging.INFO,
                "polza.image_generation.poll",
                draft_id=draft_id,
                media_id=media_id,
                status=status,
                attempt=attempt,
                elapsed_ms=elapsed_ms,
                status_code=response.status_code,
                response_json=response_json if is_full_debug_enabled() else None,
            )
            if status == "completed":
                if cost_tracker:
                    cost_tracker.record_response(
                        response_json,
                        event="image_generation.completed",
                        charge_id=media_id,
                    )
                image_url = extract_image_url_or_none(response_json)
                if image_url:
                    return image_url
                log_event(
                    logger,
                    logging.ERROR,
                    "polza.image_generation.invalid_completed_response",
                    draft_id=draft_id,
                    media_id=media_id,
                    response_json=response_json if is_full_debug_enabled() else None,
                )
                raise ValueError("Polza completed image generation response missing image URL")
            if status in {"failed", "cancelled", "canceled"}:
                error_payload = response_json.get("error") if isinstance(response_json, dict) else response_json
                log_event(
                    logger,
                    logging.ERROR,
                    "polza.image_generation.failed",
                    draft_id=draft_id,
                    media_id=media_id,
                    status=status,
                    error_payload=error_payload,
                    response_json=response_json if is_full_debug_enabled() else None,
                )
                raise RuntimeError(f"Polza image generation failed: {error_payload}")
            image_url = extract_image_url_or_none(response_json)
            if image_url and status not in {"pending", "processing"}:
                return image_url
            if attempt < max_attempts:
                await asyncio.sleep(MEDIA_POLL_INTERVAL_SECONDS)

        log_event(
            logger,
            logging.ERROR,
            "polza.image_generation.timeout",
            draft_id=draft_id,
            media_id=media_id,
            elapsed_ms=_duration_ms(started_at),
            max_wait_seconds=MEDIA_POLL_MAX_WAIT_SECONDS,
        )
        raise TimeoutError(f"Polza image generation timed out after {MEDIA_POLL_MAX_WAIT_SECONDS:g} seconds")

    async def download_image(self, url: str, media_dir: Path, stem: str) -> Path:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "polza.image_download.start", url=url, media_dir=media_dir, stem=stem)
        async with httpx.AsyncClient(timeout=60.0) as download_client:
            response = await download_client.get(url)
            response.raise_for_status()
        suffix = ".jpg"
        content_type = response.headers.get("content-type", "")
        if "png" in content_type:
            suffix = ".png"
        path = media_dir / f"{stem}{suffix}"
        path.write_bytes(response.content)
        log_event(
            logger,
            logging.INFO,
            "polza.image_download.success",
            url=url,
            path=path,
            content_type=content_type,
            size_bytes=len(response.content),
            duration_ms=_duration_ms(started_at),
        )
        return path

    async def _chat_json_with_cost(
        self,
        messages: list[dict[str, object]],
        cost_tracker: PolzaCostTracker | None = None,
    ) -> str:
        try:
            return await self._chat_json(messages, cost_tracker=cost_tracker)
        except TypeError as exc:
            if "cost_tracker" not in str(exc):
                raise
            return await self._chat_json(messages)

    async def _chat_json(
        self,
        messages: list[dict[str, object]],
        cost_tracker: PolzaCostTracker | None = None,
    ) -> str:
        started_at = time.perf_counter()
        request_json = {
            "model": self.text_model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        log_event(
            logger,
            logging.INFO,
            "polza.chat.request",
            endpoint="/chat/completions",
            model=self.text_model,
            request_json=request_json if is_full_debug_enabled() else {"model": self.text_model, "message_count": len(messages)},
        )
        response = await self.client.post(
            "/chat/completions",
            json=request_json,
        )
        response.raise_for_status()
        response_json = response.json()
        if cost_tracker:
            cost_tracker.record_response(response_json, event="chat_completion")
        content = response_json["choices"][0]["message"]["content"]
        log_event(
            logger,
            logging.INFO,
            "polza.chat.response",
            endpoint="/chat/completions",
            model=self.text_model,
            status_code=response.status_code,
            duration_ms=_duration_ms(started_at),
            response_json=response_json if is_full_debug_enabled() else None,
            content=content if is_full_debug_enabled() else None,
        )
        return content


def parse_json_object(content: str) -> dict:
    data = parse_json_value(content)
    if not isinstance(data, dict):
        log_event(logger, logging.ERROR, "polza.json_parse.error", content=content if is_full_debug_enabled() else None)
        raise ValueError("Expected JSON object from polza.ai")
    return data


def tracker_from_draft(draft: CardDraft) -> PolzaCostTracker:
    return PolzaCostTracker(
        amount_rub=draft.polza_cost_rub,
        complete=draft.polza_cost_complete,
        available=draft.polza_cost_available,
    )


def cost_fields(cost_tracker: PolzaCostTracker) -> dict[str, object]:
    return {
        "polza_cost_rub": cost_tracker.amount_rub,
        "polza_cost_complete": cost_tracker.complete,
        "polza_cost_available": cost_tracker.available,
    }


def validate_card_data(data: dict) -> None:
    missing = [field for field in REQUIRED_CARD_FIELDS if not str(data.get(field) or "").strip()]
    if missing:
        raise ValueError(f"Missing required card fields: {', '.join(missing)}")


def needs_verb_forms(data: dict, meaning: MeaningOption | None) -> bool:
    word = str(data.get("word") or "").strip()
    if not word or " " in word:
        return False
    if str(data.get("article") or "").strip():
        return False
    if str(data.get("unit_type") or "").strip() != UnitType.WORD.value:
        return False
    if meaning is None:
        return False
    return meaning.part_of_speech.casefold() == "verb"


def validate_verb_forms_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    if CARD_HTML_TAG_RE.search(text):
        raise ValueError("verb_forms must be plain text without HTML")
    if text.count(" / ") != 2:
        raise ValueError("verb_forms must use format Infinitiv / Präteritum / Perfekt")
    parts = [part.strip() for part in text.split(" / ")]
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("verb_forms must contain three non-empty forms")
    return " / ".join(parts)


def normalize_card_html_data(data: dict) -> dict:
    for field_name in CARD_HTML_DATA_FIELDS:
        value = data.get(field_name)
        if isinstance(value, str) and value:
            normalized = normalize_card_html(value)
            if normalized != value:
                data[field_name] = normalized
                log_event(
                    logger,
                    logging.INFO,
                    "polza.card_html.normalized",
                    field_name=field_name,
                    original=value if is_full_debug_enabled() else None,
                    normalized=normalized if is_full_debug_enabled() else None,
                )
    return data


def normalize_card_html(value: str) -> str:
    def replace_tag(match: re.Match[str]) -> str:
        closing, raw_tag, _attrs = match.groups()
        tag = raw_tag.lower()
        if tag == "br":
            return " "
        if tag == "strong":
            return "</b>" if closing else "<b>"
        if tag in STRIP_CARD_HTML_TAGS:
            return ""
        if tag in ALLOWED_CARD_HTML_TAGS:
            return f"</{tag}>" if closing else f"<{tag}>"
        return match.group(0)

    return re.sub(r"\s+", " ", CARD_HTML_TAG_RE.sub(replace_tag, value)).strip()


def validate_generated_draft(draft: CardDraft) -> None:
    fields = draft.anki_fields()
    missing = [
        field
        for field in (
            "SentenceDE",
            "Word",
            "TranslationRU",
            "SentenceRU",
            "MeaningRU",
            "DefinitionRU",
            "GrammarTable",
            "Level",
        )
        if not fields[field]
    ]
    if missing:
        raise ValueError(f"Generated card has empty Anki fields: {', '.join(missing)}")
    validate_card_html(fields)
    validate_card_richness(draft)


def validate_card_html(fields: dict[str, str]) -> None:
    for field_name in ("SentenceDE", "SentenceRU", *STRUCTURED_CARD_FIELDS):
        value = fields.get(field_name, "")
        for match in CARD_HTML_TAG_RE.finditer(value):
            tag = match.group(2).lower()
            if tag not in ALLOWED_CARD_HTML_TAGS:
                raise ValueError(f"Unsupported HTML tag in {field_name}: {tag}")
            attrs = match.group(3).strip()
            if attrs:
                raise ValueError(f"Unsupported HTML attributes in {field_name}: {tag}")


def validate_card_richness(draft: CardDraft) -> None:
    if draft.verb_forms:
        validate_verb_forms_text(draft.verb_forms)

    grammar = draft.grammar_table or draft.grammar_note_ru
    if len(_plain_text(grammar)) < 100 or not _has_structured_items(grammar, minimum=4):
        raise ValueError("grammar_table is too short or lacks structured learning details")

    if len(_plain_text(draft.etymology)) < 30:
        raise ValueError("etymology is too short; explain origin, morphology, or uncertainty")

    if draft.synonyms and (
        not _is_html_list(draft.synonyms) or len(_plain_text(draft.synonyms)) < 25
    ):
        raise ValueError("synonyms must be an explained HTML list or an empty string")

    if draft.other_meanings_ru and not _is_html_list(draft.other_meanings_ru):
        raise ValueError("other_meanings_ru must be an HTML list or an empty string")


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _is_html_list(value: str) -> bool:
    return bool(re.search(r"<\s*ul\s*>", value, re.IGNORECASE)) and bool(
        re.search(r"<\s*li\s*>", value, re.IGNORECASE)
    )


def _has_structured_items(value: str, minimum: int) -> bool:
    starts_with_structure = bool(re.search(r"<\s*(?:ul|table)\s*>", value, re.IGNORECASE))
    item_count = len(re.findall(r"<\s*(?:li|tr)\s*>", value, re.IGNORECASE))
    return starts_with_structure and item_count >= minimum


def parse_json_value(content: str) -> object:
    try:
        data = json.loads(content)
        if isinstance(data, str):
            data = json.loads(data)
        return data
    except json.JSONDecodeError:
        log_event(logger, logging.ERROR, "polza.json_parse.error", content=content if is_full_debug_enabled() else None, exc_info=True)
        raise


def parse_meaning_options(data: object, fallback_text: str) -> list[MeaningOption]:
    raw_options = _raw_meaning_options(data)
    if not isinstance(raw_options, list):
        raw_options = []
    options: list[MeaningOption] = []
    for item in raw_options[:5]:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        raw_unit_type = str(item.get("unit_type") or "").strip().casefold()
        if raw_unit_type == "verb" and not item.get("part_of_speech"):
            item["part_of_speech"] = "verb"
        for field in ("part_of_speech", "word", "article", "translation_ru", "meaning_ru", "short_label", "explanation_ru"):
            if item.get(field) is None:
                item[field] = ""
        if item.get("unit_type") not in {"word", "collocation", "phrase"}:
            item["unit_type"] = "word"
        if item["unit_type"] == "word" and " " in str(item.get("word", "")).strip():
            item["unit_type"] = "phrase"
        option = MeaningOption.model_validate(item)
        if option.word or option.translation_ru or option.meaning_ru:
            if not option.short_label:
                option = option.model_copy(update={"short_label": _meaning_label(option)})
            options.append(option)
    if options:
        return options
    fallback = MeaningOption(word=fallback_text, translation_ru=fallback_text, meaning_ru=fallback_text, short_label=fallback_text)
    log_event(logger, logging.WARNING, "polza.meanings.fallback", fallback_text=fallback_text, data=data if is_full_debug_enabled() else None)
    return [fallback]


def parse_meaning_selection(data: object, fallback_text: str) -> MeaningSelection:
    options = parse_meaning_options(data, fallback_text=fallback_text)
    target_source_text = _meaning_target_source_text(data)
    return MeaningSelection(source_text_for_card=target_source_text or fallback_text, options=options)


def _raw_meaning_options(data: object) -> object:
    if isinstance(data, dict):
        return data.get("options")
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and "options" in data[0]:
        return data[0].get("options")
    return data


def _meaning_target_source_text(data: object) -> str:
    if isinstance(data, dict):
        return str(data.get("target_source_text") or "").strip()
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return str(data[0].get("target_source_text") or "").strip()
    return ""


def parse_audio_meaning_selection(data: object, fallback_text: str) -> MeaningSelection:
    selection = parse_meaning_selection(data, fallback_text=fallback_text)
    fallback = fallback_text.strip()
    if not fallback:
        return selection
    if not selection.options:
        return MeaningSelection(source_text_for_card=fallback, options=parse_meaning_options([], fallback_text=fallback))

    first = selection.options[0]
    fallback_word_count = len(fallback.split())
    first_word_count = len(first.word.split())
    if fallback_word_count > 1 and first_word_count <= 1:
        first = first.model_copy(
            update={
                "unit_type": UnitType.PHRASE,
                "word": fallback,
                "article": "",
                "short_label": first.short_label or "Полная фраза из аудио",
                "explanation_ru": first.explanation_ru or "Полная фраза из аудио.",
            }
        )
        options = [first, *selection.options[1:]]
        return MeaningSelection(source_text_for_card=fallback, options=options)
    if fallback_word_count > 1 and first.unit_type == UnitType.WORD:
        first = first.model_copy(update={"unit_type": UnitType.PHRASE})
        return MeaningSelection(source_text_for_card=selection.source_text_for_card or fallback, options=[first, *selection.options[1:]])
    return selection


def _meaning_label(option: MeaningOption) -> str:
    parts = []
    word = f"{option.article} {option.word}".strip()
    if word:
        parts.append(word)
    if option.translation_ru:
        parts.append(option.translation_ru)
    return " / ".join(parts) or option.meaning_ru or "Значение"


def extract_image_url(response_json: object) -> str:
    image_url = extract_image_url_or_none(response_json)
    if image_url:
        return image_url
    raise ValueError("Polza image generation response missing image URL")


def extract_image_url_or_none(response_json: object) -> str | None:
    for value in _walk_values(response_json):
        if isinstance(value, dict):
            for key in ("url", "image_url"):
                candidate = value.get(key)
                if _is_http_url(candidate):
                    return candidate
        elif _is_http_url(value):
            return value
        elif isinstance(value, str):
            parsed = _parse_json_string(value)
            if parsed is not None:
                nested_url = extract_image_url_or_none(parsed)
                if nested_url:
                    return nested_url
    return None


def extract_media_id(response_json: object) -> str | None:
    if not isinstance(response_json, dict):
        return None
    for key in ("requestId", "id"):
        value = response_json.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def image_path_to_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def audio_path_to_data_url(audio_path: Path) -> str:
    mime_type = mimetypes.guess_type(audio_path.name)[0] or "audio/ogg"
    encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _walk_values(value: object):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)


def _is_http_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _parse_json_string(value: str) -> object | None:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _duration_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)
