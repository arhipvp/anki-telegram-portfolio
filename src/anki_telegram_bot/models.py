from enum import StrEnum
from pathlib import Path
from decimal import Decimal

from pydantic import BaseModel, Field


class UnitType(StrEnum):
    WORD = "word"
    COLLOCATION = "collocation"
    PHRASE = "phrase"


class FrequencyBand(StrEnum):
    VERY_COMMON = "very_common"
    COMMON = "common"
    MEDIUM = "medium"
    RARE = "rare"
    SPECIAL = "special"


class ImageSource(StrEnum):
    WIKIMEDIA = "wikimedia"
    POLZA_GENERATED = "polza_generated"
    TELEGRAM_PHOTO = "telegram_photo"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"


class MeaningOption(BaseModel):
    unit_type: UnitType = UnitType.WORD
    part_of_speech: str = ""
    word: str = ""
    article: str = ""
    translation_ru: str = ""
    meaning_ru: str = ""
    short_label: str = ""
    explanation_ru: str = ""


class MeaningSelection(BaseModel):
    source_text_for_card: str
    options: list[MeaningOption]
    polza_cost_rub: Decimal = Decimal("0")
    polza_cost_complete: bool = True
    polza_cost_available: bool = False


class CardDraft(BaseModel):
    id: int | None = None
    user_id: int
    language: str = "de"
    source_text: str
    unit_type: UnitType = UnitType.WORD
    word_or_phrase_de: str = ""
    translation_ru: str = ""
    example_de: str = ""
    example_ru: str = ""
    frequency_band: FrequencyBand = FrequencyBand.MEDIUM
    grammar_note_ru: str = ""
    sentence_de: str = ""
    word: str = ""
    article: str = ""
    gender_color: str = ""
    level: str = ""
    sentence_ru: str = ""
    meaning_ru: str = ""
    other_meanings_ru: str = ""
    definition_ru: str = ""
    synonyms: str = ""
    grammar_table: str = ""
    verb_forms: str = ""
    etymology: str = ""
    style_and_group: str = ""
    image_query: str = ""
    image_path: Path | None = None
    image_source: ImageSource | None = None
    audio_path: Path | None = None
    polza_cost_rub: Decimal = Decimal("0")
    polza_cost_complete: bool = True
    polza_cost_available: bool = False
    status: DraftStatus = DraftStatus.DRAFT

    def preview_text(self) -> str:
        word = self.word or self.word_or_phrase_de
        sentence_de = self.sentence_de or self.example_de
        sentence_ru = self.sentence_ru or self.example_ru
        article = f"{self.article} " if self.article else ""
        grammar = self.grammar_table or self.grammar_note_ru
        rows = [
            ("Исходный ввод", self.source_text),
            ("Тип", self.unit_type.value),
            ("Слово / фраза", f"{article}{word}".strip()),
            ("Артикль", self.article),
            ("Род / цвет", self.gender_color),
            ("Перевод", self.translation_ru),
            ("Предложение DE", sentence_de),
            ("Предложение RU", sentence_ru),
            ("Значение", self.meaning_ru),
            ("Другие значения", self.other_meanings_ru),
            ("Определение", self.definition_ru),
            ("Синонимы", self.synonyms),
            ("Грамматика", grammar),
            ("Формы глагола", self.verb_forms),
            ("Этимология / морфология", self.etymology),
            ("Стиль / тематика", self.style_and_group),
            ("Уровень", self.level),
            ("Частотность", self.frequency_band.value),
            ("Image query", self.image_query),
            ("Источник картинки", self.image_source.value if self.image_source else "no_image"),
            ("Голосовое аудио", self.audio_path.name if self.audio_path else "—"),
        ]
        parts = ["Черновик карточки:"]
        for label, value in rows:
            parts.append(f"{label}: {value or '—'}")
        return "\n\n".join(parts)

    def anki_fields(self, image_html: str = "", user_audio: str = "") -> dict[str, str]:
        word = self.word or self.word_or_phrase_de
        return {
            "SentenceDE": self.sentence_de or self.example_de,
            "Word": word,
            "Article": self.article,
            "GenderColor": self.gender_color,
            "Level": self.level,
            "TranslationRU": self.translation_ru,
            "SentenceRU": self.sentence_ru or self.example_ru,
            "Image": image_html,
            "MeaningRU": self.meaning_ru,
            "OtherMeaningsRU": self.other_meanings_ru,
            "DefinitionRU": self.definition_ru,
            "Synonyms": self.synonyms,
            "GrammarTable": self.grammar_table or self.grammar_note_ru,
            "VerbForms": self.verb_forms,
            "Etymology": self.etymology,
            "StyleAndGroup": self.style_and_group,
            "ImageQuery": self.image_query,
            "FrequencyBand": self.frequency_band.value,
            "SourceText": self.source_text,
            "UserAudio": user_audio,
        }


class WikimediaImageCandidate(BaseModel):
    title: str
    image_url: str
    page_url: str | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None


class ImageEvaluation(BaseModel):
    is_match: bool = Field(description="Whether image matches the target card concept.")
    reason: str
