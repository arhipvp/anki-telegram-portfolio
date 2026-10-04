from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    language: str
    deck_name: str

    @property
    def note_type(self) -> str:
        return {"de": "German Context v1", "en": "English Context v1", "ko": "Korean Context v1"}.get(
            self.language, f"{self.language.upper()} Context v1"
        )

    @property
    def language_name(self) -> str:
        return {"de": "немецкий", "en": "английский", "ko": "корейский"}.get(self.language, self.language)

    @property
    def language_name_dative(self) -> str:
        return {"de": "немецкому", "en": "английскому", "ko": "корейскому"}.get(self.language, self.language)

    @property
    def anki_tag(self) -> str:
        return {"de": "german", "en": "english", "ko": "korean"}.get(self.language, self.language)


@dataclass(frozen=True)
class UserProfile:
    user_id: int
    languages: dict[str, LanguageProfile]

    @property
    def default_language(self) -> str:
        return "de" if "de" in self.languages else next(iter(self.languages), "de")

    def get(self, language: str | None) -> LanguageProfile:
        return self.languages.get(language or "", self.languages[self.default_language])


def parse_user_profiles(raw: str, default_user_ids: set[int], default_deck: str) -> dict[int, UserProfile]:
    profiles: dict[int, UserProfile] = {}
    for user_id in default_user_ids:
        profiles[user_id] = UserProfile(user_id=user_id, languages={"de": LanguageProfile("de", default_deck)})
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) != 3:
            raise ValueError("TELEGRAM_USER_PROFILES must use user_id:language:deck_name entries")
        user_id_text, language, deck_name = (part.strip() for part in parts)
        if not user_id_text or not language or not deck_name:
            raise ValueError("TELEGRAM_USER_PROFILES contains an empty user, language, or deck")
        user_id = int(user_id_text)
        language = language.lower()
        if language not in {"de", "en", "ko"}:
            raise ValueError(f"Unsupported language profile: {language}")
        current = profiles.get(user_id)
        languages = dict(current.languages) if current else {}
        languages[language] = LanguageProfile(language, deck_name)
        profiles[user_id] = UserProfile(user_id=user_id, languages=languages)
    return profiles
