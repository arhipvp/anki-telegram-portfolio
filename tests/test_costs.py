from decimal import Decimal

import pytest

from anki_telegram_bot.costs import PolzaCostTracker, extract_cost_rub, format_polza_cost


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"usage": {"cost_rub": 1.45}}, Decimal("1.45")),
        ({"usage": {"cost": "2.10"}}, Decimal("2.10")),
        ({"cost_rub": "3.25"}, Decimal("3.25")),
        ({"cost": 4}, Decimal("4")),
        ({"usage": {}}, None),
    ],
)
def test_extract_cost_rub_uses_supported_fallbacks(response: dict, expected: Decimal | None) -> None:
    assert extract_cost_rub(response) == expected


def test_cost_tracker_sums_responses_and_deduplicates_charge_id() -> None:
    tracker = PolzaCostTracker()

    tracker.record_response({"usage": {"cost_rub": 1.1}}, event="chat", charge_id="same")
    tracker.record_response({"usage": {"cost_rub": 1.1}}, event="poll", charge_id="same")
    tracker.record_response({"usage": {"cost_rub": 0.35}}, event="chat", charge_id="other")

    assert tracker.amount_rub == Decimal("1.45")
    assert tracker.available is True
    assert tracker.complete is True


def test_cost_tracker_marks_missing_cost_as_incomplete() -> None:
    tracker = PolzaCostTracker()

    tracker.record_response({"id": "no-cost"}, event="chat")

    assert tracker.available is False
    assert tracker.complete is False


@pytest.mark.parametrize(
    ("amount", "complete", "available", "expected"),
    [
        (Decimal("1.445"), True, True, "Стоимость: 1,45 руб."),
        (Decimal("1.451"), False, True, "Стоимость: не менее 1,45 руб."),
        (Decimal("0"), True, False, "Стоимость: недоступна"),
    ],
)
def test_format_polza_cost(
    amount: Decimal,
    complete: bool,
    available: bool,
    expected: str,
) -> None:
    assert format_polza_cost(amount, complete=complete, available=available) == expected
