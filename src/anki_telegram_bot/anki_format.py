NOTE_TYPE_NAME = "German Context v1"
LEGACY_SOURCE_NOTE_TYPE = "n8n v.2"

FIELDS = [
    "SentenceDE",
    "Word",
    "Article",
    "GenderColor",
    "Level",
    "TranslationRU",
    "SentenceRU",
    "Image",
    "MeaningRU",
    "OtherMeaningsRU",
    "DefinitionRU",
    "Synonyms",
    "GrammarTable",
    "VerbForms",
    "Etymology",
    "StyleAndGroup",
    "ImageQuery",
    "FrequencyBand",
    "SourceText",
    "UserAudio",
]

CSS = """/* my_anki: German Context v1 (css-only) */
:root {
  --bg: #f6f7fb;
  --card: #ffffff;
  --text: #111827;
  --muted: #6b7280;
  --border: #e5e7eb;
}

html { background: var(--bg); }

.card {
  font-family: -apple-system, Segoe UI, Inter, Arial, sans-serif;
  font-size: 20px;
  text-align: left;
  line-height: 1.55;
  color: var(--text);
  background: var(--card);
  max-width: 820px;
  margin: 18px auto;
  border: 1px solid rgba(229,231,235,0.9);
  border-radius: 18px;
  padding: 22px 22px 18px;
  box-shadow: 0 10px 30px rgba(17,24,39,0.10);
}

@media (max-width: 860px) {
  .card {
    margin: 0;
    border-radius: 0;
    border-left: none;
    border-right: none;
    max-width: none;
  }
}

.sentence-de {
  font-size: 28px;
  font-weight: 500;
  letter-spacing: 0;
  margin: 0 0 10px 0;
}

.sentence-ru {
  font-size: 21px;
  color: #374151;
  margin: 10px 0 0 0;
}

.sentence-de b,
.sentence-ru b {
  font-weight: 800;
  color: var(--text);
}

.audio {
  margin: 8px 0 14px 0;
}

.image {
  margin-top: 14px;
}

.image img {
  display: block;
  max-height: 320px;
  max-width: 100%;
  object-fit: contain;
  border-radius: 14px;
  border: 1px solid rgba(229,231,235,0.9);
  box-shadow: 0 8px 18px rgba(17,24,39,0.12);
}

hr#answer {
  border: none;
  height: 1px;
  background: rgba(229,231,235,0.9);
  margin: 18px 0;
}

.word {
  font-size: 40px;
  font-weight: 800;
  margin: 0 0 10px 0;
  letter-spacing: 0;
}

.m { color: #2563eb; }
.f { color: #dc2626; }
.n { color: #16a34a; }

.block {
  margin-top: 12px;
  padding: 12px 14px;
  background: #f9fafb;
  border: 1px solid rgba(229,231,235,0.9);
  border-radius: 14px;
}

.block-title {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 6px;
}

.small {
  font-size: 19px;
  color: var(--text);
}

.small table {
  width: 100%;
  border-collapse: collapse;
}

.small td {
  padding: 6px 8px;
  border-top: 1px solid rgba(229,231,235,0.8);
}

.small tr:first-child td {
  border-top: none;
}

.small ul {
  margin: 8px 0 0 18px;
}

.small li {
  margin: 4px 0;
}

/* my_anki: keyword-highlight (keep) */
.sentence-de b { font-weight: 800; }
"""

CARD_1_FRONT = """{{#SentenceDE}}
<div class="sentence-de">
    {{SentenceDE}}
</div>

<div class="audio">
    {{tts de_DE voices=com.google.android.tts-de-de-x-dea-local:SentenceDE}}
</div>
{{/SentenceDE}}

{{#Image}}
<div class="image">
    {{Image}}
</div>
{{/Image}}
"""

CARD_BACK = """<script>try{window.scrollTo(0,0);document.body.scrollTop=0;document.documentElement.scrollTop=0;}catch(e){}</script>
<div class="sentence-de">
    {{SentenceDE}}
</div>

<div class="audio">
    {{tts de_DE voices=com.google.android.tts-de-de-x-dea-local:SentenceDE}}
</div>

<div class="sentence-ru">
    {{SentenceRU}}
</div>

<hr id="answer">

<div class="word {{GenderColor}}">
    {{^VerbForms}}{{#Article}}{{Article}} {{/Article}}{{/VerbForms}}{{Word}}{{#VerbForms}} / {{VerbForms}}{{/VerbForms}}
</div>

{{#TranslationRU}}
<div class="block">
    <div class="block-title">Перевод</div>
    <div class="small">{{TranslationRU}}</div>
</div>
{{/TranslationRU}}

{{#MeaningRU}}
<div class="block">
    <div class="block-title">Значение</div>
    {{MeaningRU}}
</div>
{{/MeaningRU}}

{{#OtherMeaningsRU}}
<div class="block">
    <div class="block-title">Другие значения</div>
    <div class="small">{{OtherMeaningsRU}}</div>
</div>
{{/OtherMeaningsRU}}

{{#DefinitionRU}}
<div class="block">
    <div class="block-title">Определение</div>
    <div class="small">{{DefinitionRU}}</div>
</div>
{{/DefinitionRU}}

{{#Synonyms}}
<div class="block">
    <div class="block-title">Синонимы (DE)</div>
    <div class="small">{{Synonyms}}</div>
</div>
{{/Synonyms}}

{{#GrammarTable}}
<div class="block">
    <div class="block-title">Грамматика</div>
    <div class="small">{{GrammarTable}}</div>
</div>
{{/GrammarTable}}

{{#Etymology}}
<div class="block">
    <div class="block-title">Этимология / морфология</div>
    <div class="small">{{Etymology}}</div>
</div>
{{/Etymology}}

{{#StyleAndGroup}}
<div class="block">
    <div class="block-title">Стиль / тематика</div>
    <div class="small">{{StyleAndGroup}}</div>
</div>
{{/StyleAndGroup}}

{{#FrequencyBand}}
<div class="block">
    <div class="block-title">Частотность</div>
    <div class="small">{{FrequencyBand}}</div>
</div>
{{/FrequencyBand}}

{{#SourceText}}
<div class="block">
    <div class="block-title">Исходный ввод</div>
    <div class="small">{{SourceText}}</div>
</div>
{{/SourceText}}

{{#UserAudio}}
<div class="block">
    <div class="block-title">Голос пользователя</div>
    <div class="small">{{UserAudio}}</div>
</div>
{{/UserAudio}}

{{#Level}}
<div class="block">
    <div class="block-title">Уровень</div>
    <div class="small">{{Level}}</div>
</div>
{{/Level}}
"""

CARD_2_FRONT = """{{#SentenceRU}}
<div class="sentence-de">
    {{SentenceRU}}
</div>
{{/SentenceRU}}

{{#Image}}
<div class="image">
    {{Image}}
</div>
{{/Image}}
"""

CARD_TEMPLATES = [
    {"Name": "Карточка 1", "Front": CARD_1_FRONT, "Back": CARD_BACK},
    {"Name": "Карточка 2", "Front": CARD_2_FRONT, "Back": CARD_BACK},
]


def format_for_language(language: str) -> tuple[str, list[str], str, list[dict[str, str]]]:
    """Return note type name, fields, CSS, and templates for a target language.

    The internal CardDraft field names stay stable for old SQLite drafts; the
    note type and learner-facing template are language-specific.
    """
    if language == "de":
        return NOTE_TYPE_NAME, FIELDS, CSS, CARD_TEMPLATES
    if language in {"en", "ko"}:
        locale = "en_US" if language == "en" else "ko_KR"
        synonym_label = "Синонимы (EN)" if language == "en" else "Синонимы (KO)"
        template_name = "English Context v1" if language == "en" else "Korean Context v1"
        templates = [
            {"Name": "Card 1", "Front": CARD_1_FRONT.replace("de_DE voices=com.google.android.tts-de-de-x-dea-local:", f"{locale}:"), "Back": CARD_BACK.replace("de_DE voices=com.google.android.tts-de-de-x-dea-local:", f"{locale}:").replace("Синонимы (DE)", synonym_label)},
            {"Name": "Card 2", "Front": CARD_2_FRONT, "Back": CARD_BACK.replace("de_DE voices=com.google.android.tts-de-de-x-dea-local:", f"{locale}:").replace("Синонимы (DE)", synonym_label)},
        ]
        return template_name, FIELDS, CSS.replace("German Context v1", template_name), templates
    raise ValueError(f"Unsupported Anki language: {language}")
