import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .logging_config import log_event


logger = logging.getLogger(__name__)


@dataclass
class PolzaCostTracker:
    amount_rub: Decimal = Decimal("0")
    complete: bool = True
    available: bool = False
    charged_ids: set[str] = field(default_factory=set)

    def record_response(
        self,
        response_json: object,
        *,
        event: str,
        charge_id: str | None = None,
        missing_cost_is_incomplete: bool = True,
    ) -> None:
        resolved_id = charge_id or _response_id(response_json)
        if resolved_id and resolved_id in self.charged_ids:
            return

        cost = extract_cost_rub(response_json)
        if cost is None:
            if missing_cost_is_incomplete:
                self.complete = False
                log_event(
                    logger,
                    logging.WARNING,
                    "polza.cost.missing",
                    source_event=event,
                    charge_id=resolved_id,
                )
            return

        self.amount_rub += cost
        self.available = True
        if resolved_id:
            self.charged_ids.add(resolved_id)
        log_event(
            logger,
            logging.INFO,
            "polza.cost.recorded",
            source_event=event,
            charge_id=resolved_id,
            cost_rub=str(cost),
            total_cost_rub=str(self.amount_rub),
        )


def extract_cost_rub(response_json: object) -> Decimal | None:
    if not isinstance(response_json, dict):
        return None
    usage = response_json.get("usage")
    candidates: list[object] = []
    if isinstance(usage, dict):
        candidates.extend((usage.get("cost_rub"), usage.get("cost")))
    candidates.extend((response_json.get("cost_rub"), response_json.get("cost")))
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            cost = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if cost >= 0:
            return cost
    return None


def format_polza_cost(amount_rub: Decimal, *, complete: bool, available: bool) -> str:
    if not available:
        return "Стоимость: недоступна"
    rounded = amount_rub.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    value = f"{rounded:.2f}".replace(".", ",")
    prefix = "" if complete else "не менее "
    return f"Стоимость: {prefix}{value} руб."


def _response_id(response_json: object) -> str | None:
    if not isinstance(response_json, dict):
        return None
    value = response_json.get("id") or response_json.get("requestId") or response_json.get("request_id")
    return str(value) if value else None
