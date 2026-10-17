"""Deterministic validation of every LLM finding. The model proposes; this code disposes.

Checks, in order:
1. Schema: the payload parses as AgentFinding (enum, 2-4 sentence explanation, confidence 0..1).
2. Evidence: every cited id exists in the run's source data, including ids mentioned in prose.
3. Side: the break type is possible for the ledger the item sits on.
4. Sign: deposits in transit and interest are inflows; checks, fees, and NSF returns are outflows.
5. Pairing: transpositions and wrong amounts name an unmatched counterpart on the other ledger, and
   the arithmetic claim is re-verified (a digit swap really is a digit swap).
6. Duplicates: a GL duplicate must have a same-amount twin in the GL.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from tieout.agent.policy import ExceptionContext
from tieout.agent.tools import LedgerView
from tieout.generator import is_digit_swap
from tieout.models import BREAK_SIDE, BREAK_SIGN, PAIRED_BREAKS, AgentFinding, BreakType, Side
from tieout.money import money_to_cents
from tieout.similarity import similarity


class FindingValidationError(ValueError):
    """The finding failed a deterministic control. Message is fed back to the model on retry."""


def _id_pattern(view: LedgerView) -> re.Pattern[str]:
    prefixes = sorted({i.split("-")[0] for i in [*view.bank, *view.gl] if "-" in i})
    if not prefixes:
        return re.compile(r"(?!x)x")
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in prefixes) + r")-\d+\b")


def parse_finding(payload: dict[str, Any] | AgentFinding) -> AgentFinding:
    if isinstance(payload, AgentFinding):
        return payload
    try:
        return AgentFinding.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'finding'}: {e['msg']}" for e in exc.errors()
        )
        raise FindingValidationError(f"schema validation failed: {problems}") from exc


def validate_finding(
    payload: dict[str, Any] | AgentFinding, ctx: ExceptionContext, view: LedgerView
) -> AgentFinding:
    f = parse_finding(payload)
    errors: list[str] = []

    cited = list(f.evidence_ids)
    if f.counterpart_id:
        cited.append(f.counterpart_id)
    cited += _id_pattern(view).findall(f.explanation + " " + f.proposed_action)
    missing = sorted({i for i in cited if not view.exists(i)})
    if missing:
        errors.append(f"cited ids do not exist in the source data: {', '.join(missing)}")

    side = Side(ctx.ledger)
    required_side = BREAK_SIDE[f.break_type]
    if required_side is not None and required_side != side:
        errors.append(
            f"{f.break_type.value} applies to {required_side.value} items, but "
            f"{ctx.item_id} is a {side.value} item"
        )
    sign = BREAK_SIGN.get(f.break_type)
    if sign is not None and (ctx.amount > 0) != (sign > 0):
        direction = "an inflow (positive)" if sign > 0 else "an outflow (negative)"
        errors.append(f"{f.break_type.value} must be {direction}; {ctx.item_id} is {ctx.amount}")

    if f.break_type == BreakType.OUTSTANDING_CHECK:
        item = view.item(ctx.item_id)
        if not item.check_number:
            errors.append(
                f"{ctx.item_id} has no check number, so it cannot be an outstanding check"
            )

    if f.break_type in PAIRED_BREAKS:
        errors += _check_pair(f, ctx, view)
    elif f.counterpart_id:
        errors.append(f"counterpart_id is only allowed for {', '.join(sorted(PAIRED_BREAKS))}")

    if f.break_type == BreakType.GL_DUPLICATE and not missing:
        item = view.item(ctx.item_id)
        twins = [
            g
            for g in view.gl.values()
            if g.entry_id != item.item_id
            and g.amount == item.amount
            and (
                g.reference == item.reference or similarity(g.description, item.description) >= 0.9
            )
        ]
        if not twins:
            errors.append(f"no GL entry duplicates {ctx.item_id} (same amount and reference)")

    if errors:
        raise FindingValidationError("; ".join(errors))
    return f


def _check_pair(f: AgentFinding, ctx: ExceptionContext, view: LedgerView) -> list[str]:
    cp = f.counterpart_id
    if not cp:
        return [
            f"{f.break_type.value} requires counterpart_id (the same transaction in the "
            "opposite ledger)"
        ]
    if not view.exists(cp):
        return []  # already reported as a missing id
    item = view.item(ctx.item_id)
    other = view.item(cp)
    errs = []
    if other.side == item.side:
        errs.append(f"counterpart {cp} must be on the opposite ledger from {ctx.item_id}")
    if view.status(cp) != "UNMATCHED":
        errs.append(f"counterpart {cp} is already {view.status(cp).lower()}")
    if (other.amount < 0) != (item.amount < 0):
        errs.append(f"counterpart {cp} has the opposite sign")
    a, b = money_to_cents(item.amount), money_to_cents(other.amount)
    if f.break_type == BreakType.GL_TRANSPOSITION and not is_digit_swap(a, b):
        errs.append(f"{item.amount} and {other.amount} are not a two-digit transposition")
    if f.break_type == BreakType.GL_WRONG_AMOUNT and a == b:
        errs.append("amounts are equal, so this is not a wrong amount")
    return errs
