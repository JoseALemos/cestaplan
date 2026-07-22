"""AI usage metering: read REAL token usage from an OpenAI response and record it.

Server-side truth only: token counts come from the provider's ``response.usage`` object
(``input_tokens`` / ``output_tokens``), never from anything a client sends. The imputed
``estimated_cost`` is computed from a CONFIGURABLE per-model price table
(``settings.openai_price_table``); with no price table the cost stays ``NULL`` — a cost
is never fabricated. The managed API key is never read, logged or returned here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from cestaplan_api.models import UsageLedger

if TYPE_CHECKING:
    from cestaplan_api.config import Settings

_MILLION = Decimal("1000000")
_INPUT_KEYS = ("input_per_million", "input", "prompt_per_million", "prompt")
_OUTPUT_KEYS = ("output_per_million", "output", "completion_per_million", "completion")


def _token_count(usage: Any, *names: str) -> int:
    """Read a token field from an OpenAI ``response.usage`` object, defensively.

    The Responses API exposes ``input_tokens`` / ``output_tokens``; fall back across a
    couple of aliases and default to 0 when the field is absent (never trust a client).
    """
    if usage is None:
        return 0
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return 0


def extract_token_usage(response: Any) -> tuple[int, int]:
    """Return ``(input_tokens, output_tokens)`` read from ``response.usage`` (server truth)."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    input_tokens = _token_count(usage, "input_tokens", "prompt_tokens")
    output_tokens = _token_count(usage, "output_tokens", "completion_tokens")
    return input_tokens, output_tokens


def _price(prices: dict[str, str], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key in prices:
            try:
                return Decimal(str(prices[key]))
            except (ArithmeticError, ValueError):
                return None
    return None


def compute_estimated_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    price_table: dict[str, dict[str, str]],
) -> Decimal | None:
    """Impute cost from the price table, or ``None`` when the model has no entry.

    Prices are per 1,000,000 tokens. Returns ``None`` (never 0-by-fabrication) whenever
    the model is not present in the table.
    """
    prices = price_table.get(model)
    if not prices:
        return None
    input_price = _price(prices, _INPUT_KEYS)
    output_price = _price(prices, _OUTPUT_KEYS)
    if input_price is None and output_price is None:
        return None
    cost = Decimal("0")
    if input_price is not None:
        cost += (Decimal(input_tokens) / _MILLION) * input_price
    if output_price is not None:
        cost += (Decimal(output_tokens) / _MILLION) * output_price
    return cost


def record_openai_usage(
    db: Session,
    *,
    response: Any,
    settings: Settings,
    operation: str,
    household_id: int | None = None,
    user_id: int | None = None,
    optimization_run_id: int | None = None,
    currency: str = "EUR",
    extra: dict | None = None,
) -> UsageLedger:
    """Write one :class:`UsageLedger` row from a REAL OpenAI response.

    Token counts are read from ``response.usage`` server-side; ``estimated_cost`` comes
    from the configured price table (``NULL`` when the model is not priced).
    """
    input_tokens, output_tokens = extract_token_usage(response)
    estimated_cost = compute_estimated_cost(
        settings.openai_model, input_tokens, output_tokens, settings.price_table
    )
    ledger = UsageLedger(
        user_id=user_id,
        household_id=household_id,
        optimization_run_id=optimization_run_id,
        operation=operation or "openai_call",
        model=settings.openai_model or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=estimated_cost,
        currency=currency,
        extra=extra,
    )
    db.add(ledger)
    db.flush()
    return ledger
