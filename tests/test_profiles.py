from anki_telegram_bot.profiles import parse_user_profiles


def test_parse_user_profiles_supports_multiple_languages_per_user() -> None:
    profiles = parse_user_profiles(
        "1:de:German,2:de:LearnerTwo::German,2:en:LearnerTwo::English,3:ko:LearnerThree::Korean",
        set(),
        "German",
    )

    assert profiles[1].default_language == "de"
    assert set(profiles[2].languages) == {"de", "en"}
    assert profiles[2].languages["en"].deck_name == "LearnerTwo::English"
    assert profiles[3].languages["ko"].note_type == "Korean Context v1"


def test_parse_user_profiles_preserves_legacy_default_users() -> None:
    profiles = parse_user_profiles("", {42}, "German")

    assert profiles[42].languages["de"].deck_name == "German"
