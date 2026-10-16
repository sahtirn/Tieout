from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel

from tieout.money import Money, MoneyError, cents_to_money, fmt_money, sum_money, to_money


def test_rounding_is_half_up() -> None:
    assert to_money("1.005") == Decimal("1.01")
    assert to_money("-1.005") == Decimal("-1.01")
    assert to_money(Decimal("2.675")) == Decimal("2.68")


def test_floats_rejected() -> None:
    with pytest.raises(MoneyError):
        to_money(0.1)


@pytest.mark.parametrize("bad", ["", "abc", True, None, "NaN", [1]])
def test_bad_values_rejected(bad: object) -> None:
    with pytest.raises(MoneyError):
        to_money(bad)


def test_parsing_variants() -> None:
    assert to_money("$1,253.00") == Decimal("1253.00")
    assert to_money("(42.10)") == Decimal("-42.10")
    assert to_money(7) == Decimal("7.00")
    assert cents_to_money(125300) == Decimal("1253.00")


def test_format_and_sum() -> None:
    assert fmt_money(Decimal("-1234.5")) == "-1,234.50"
    assert fmt_money(Decimal("-1234.5"), parens=True) == "(1,234.50)"
    assert sum_money(["0.10", "0.20"]) == Decimal("0.30")


def test_pydantic_money_field_serializes_as_string() -> None:
    class M(BaseModel):
        amount: Money

    m = M(amount="10.1")
    assert m.amount == Decimal("10.10")
    assert m.model_dump_json() == '{"amount":"10.10"}'
    with pytest.raises(ValueError):
        M(amount=1.5)


def test_no_float_calls_in_source() -> None:
    """Guardrail: the codebase never converts anything to float."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "tieout"
    offenders = [p.name for p in src.rglob("*.py") if "float(" in p.read_text(encoding="utf-8")]
    assert offenders == []
