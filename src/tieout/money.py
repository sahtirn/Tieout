"""Money handling. Every amount in TieOut is a Decimal quantized to 0.01 with ROUND_HALF_UP.

Floats are rejected at the boundary so binary floating point can never leak into a balance.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class MoneyError(ValueError):
    """Raised when a value cannot be safely converted to money."""


def to_money(value: Any) -> Decimal:
    """Convert str, int, or Decimal to a Decimal quantized to cents.

    Floats are refused outright: a float has already lost precision before we see it.
    Strings may carry thousands separators, a leading currency symbol, or accounting parentheses.
    """
    if isinstance(value, bool):
        raise MoneyError("booleans are not money")
    if isinstance(value, float):
        raise MoneyError("floats are forbidden for money; pass a str, int, or Decimal")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        if text == "":
            raise MoneyError("empty string is not money")
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"not a money value: {value!r}") from exc
        if negative:
            dec = -dec
    else:
        raise MoneyError(f"unsupported money type: {type(value).__name__}")
    if not dec.is_finite():
        raise MoneyError("money must be finite")
    return dec.quantize(CENT, rounding=ROUND_HALF_UP)


def cents_to_money(cents: int) -> Decimal:
    """Build money from an integer number of cents (used by the generator, never via floats)."""
    return (Decimal(cents) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def money_to_cents(value: Decimal) -> int:
    return int(to_money(value) * 100)


def fmt_money(value: Decimal, *, parens: bool = False) -> str:
    """Format with thousands separators. Negative values use a minus sign or accounting parens."""
    value = to_money(value)
    body = f"{abs(value):,.2f}"
    if value < 0:
        return f"({body})" if parens else f"-{body}"
    return body


def sum_money(values: Any) -> Decimal:
    total = ZERO
    for v in values:
        total += to_money(v)
    return to_money(total)


Money = Annotated[
    Decimal,
    BeforeValidator(to_money),
    PlainSerializer(lambda v: format(to_money(v), "f"), return_type=str, when_used="json"),
]
"""Pydantic field type for money. Serializes to a JSON string such as "1253.00"."""
