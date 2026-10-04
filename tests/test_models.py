from anki_telegram_bot.models import CardDraft, FrequencyBand, UnitType


def test_card_draft_preview_contains_main_fields() -> None:
    draft = CardDraft(
        user_id=1,
        source_text="страховка",
        unit_type=UnitType.WORD,
        word_or_phrase_de="die Versicherung",
        word="Versicherung",
        article="die",
        translation_ru="страховка",
        example_de="Ich brauche eine Versicherung.",
        example_ru="Мне нужна страховка.",
        sentence_de="Ich brauche <b>eine Versicherung</b>.",
        sentence_ru="Мне нужна <b>страховка</b>.",
        meaning_ru="договор защиты от риска",
        other_meanings_ru="страхование",
        definition_ru="Eine Versicherung schützt vor Risiken.",
        synonyms="Assekuranz",
        level="B1",
        frequency_band=FrequencyBand.MEDIUM,
        grammar_note_ru="Существительное женского рода.",
        verb_forms="",
        etymology="от sichern",
        style_and_group="быт, документы",
        image_query="insurance contract",
    )

    preview = draft.preview_text()

    assert "die Versicherung" in preview
    assert "Мне нужна <b>страховка</b>." in preview
    assert "Исходный ввод: страховка" in preview
    assert "Тип: word" in preview
    assert "Значение: договор защиты от риска" in preview
    assert "Другие значения: страхование" in preview
    assert "Определение: Eine Versicherung schützt vor Risiken." in preview
    assert "Синонимы: Assekuranz" in preview
    assert "Формы глагола: —" in preview
    assert "Этимология / морфология: от sichern" in preview
    assert "Стиль / тематика: быт, документы" in preview
    assert "Уровень: B1" in preview
    assert "Частотность: medium" in preview
    assert "Image query: insurance contract" in preview
    assert "Источник картинки: no_image" in preview
    assert "Голосовое аудио: —" in preview
    assert "medium" in preview
    assert "Полезность" not in preview


def test_card_draft_export_does_not_include_learning_priority() -> None:
    draft = CardDraft(user_id=1, source_text="страховка")

    assert "learning_priority" not in draft.model_dump()


def test_card_draft_anki_fields_include_user_audio() -> None:
    draft = CardDraft(user_id=1, source_text="vereinbaren", verb_forms="vereinbaren / vereinbarte / hat vereinbart")

    assert draft.anki_fields(user_audio="[sound:voice.mp3]")["UserAudio"] == "[sound:voice.mp3]"
    assert draft.anki_fields()["VerbForms"] == "vereinbaren / vereinbarte / hat vereinbart"
