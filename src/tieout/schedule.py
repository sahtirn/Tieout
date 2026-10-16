"""Bank reconciliation schedule.

    Adjusted bank balance = ending bank balance + deposits in transit - outstanding checks
                            +/- bank errors
    Adjusted book balance = ending GL balance + unrecorded bank credits - unrecorded bank debits
                            +/- book errors

All amounts are signed from the cash account's perspective, so "minus outstanding checks" is the
addition of a negative amount. Only findings that are auto-accepted or approved by a reviewer are
counted. Anything else stays visible as an unexplained item, and the difference is never plugged.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from tieout.models import (
    BreakType,
    ReconException,
    ReconStatus,
    ResolvedFinding,
    ScheduleLine,
    TieOut,
)
from tieout.money import ZERO, to_money


class ScheduleError(ValueError):
    """Raised when a finding cannot be placed on the schedule (missing counterpart, unknown id)."""


def adjustment_for(finding: ResolvedFinding, amounts: Mapping[str, Decimal]) -> Decimal:
    """Signed adjustment a finding contributes to its side of the schedule. Pure arithmetic."""
    bt = finding.finding.break_type
    item_amount = amounts[finding.item_id]
    if bt in (BreakType.DEPOSIT_IN_TRANSIT, BreakType.OUTSTANDING_CHECK):
        return item_amount
    if bt == BreakType.BANK_ERROR:
        return -item_amount
    if bt in (
        BreakType.BANK_FEE_UNRECORDED,
        BreakType.INTEREST_UNRECORDED,
        BreakType.NSF_RETURN_UNRECORDED,
    ):
        return item_amount
    if bt == BreakType.GL_DUPLICATE:
        return -item_amount
    # Transposition / wrong amount: correct the book to the bank amount.
    counterpart = finding.finding.counterpart_id
    if counterpart is None or counterpart not in amounts:
        raise ScheduleError(f"{finding.exception_id}: {bt} requires a valid counterpart")
    bank_id, gl_id = (
        (finding.item_id, counterpart)
        if finding.side.value == "BANK"
        else (counterpart, finding.item_id)
    )
    return to_money(amounts[bank_id] - amounts[gl_id])


def build_tieout(
    period: str,
    bank_ending: Decimal,
    gl_ending: Decimal,
    exceptions: list[ReconException],
    findings: list[ResolvedFinding],
    amounts: Mapping[str, Decimal],
) -> TieOut:
    groups: dict[str, list[ScheduleLine]] = {
        "dit": [],
        "os": [],
        "bank_err": [],
        "credits": [],
        "debits": [],
        "book_err": [],
    }
    covered: set[str] = set()
    for f in findings:
        if not f.counted_in_schedule or f.finding.break_type == BreakType.UNIDENTIFIED:
            continue
        bt = f.finding.break_type
        amount = adjustment_for(f, amounts)
        item_ids = [f.item_id] + ([f.finding.counterpart_id] if f.finding.counterpart_id else [])
        covered.update(item_ids)
        line = ScheduleLine(
            exception_id=f.exception_id,
            break_type=bt,
            item_ids=item_ids,
            description=f.finding.explanation,
            amount=amount,
            review_status=f.review_status,
        )
        if bt == BreakType.DEPOSIT_IN_TRANSIT:
            groups["dit"].append(line)
        elif bt == BreakType.OUTSTANDING_CHECK:
            groups["os"].append(line)
        elif bt == BreakType.BANK_ERROR:
            groups["bank_err"].append(line)
        elif bt == BreakType.INTEREST_UNRECORDED:
            groups["credits"].append(line)
        elif bt in (BreakType.BANK_FEE_UNRECORDED, BreakType.NSF_RETURN_UNRECORDED):
            groups["debits"].append(line)
        else:
            groups["book_err"].append(line)

    def total(key: str) -> Decimal:
        return to_money(sum((ln.amount for ln in groups[key]), ZERO))

    adjusted_bank = to_money(bank_ending + total("dit") + total("os") + total("bank_err"))
    adjusted_book = to_money(gl_ending + total("credits") + total("debits") + total("book_err"))
    difference = to_money(adjusted_bank - adjusted_book)
    unexplained = [e for e in exceptions if e.item_id not in covered]
    return TieOut(
        period=period,
        bank_ending_balance=bank_ending,
        gl_ending_balance=gl_ending,
        deposits_in_transit=groups["dit"],
        outstanding_checks=groups["os"],
        bank_errors=groups["bank_err"],
        unrecorded_credits=groups["credits"],
        unrecorded_debits=groups["debits"],
        book_errors=groups["book_err"],
        adjusted_bank_balance=adjusted_bank,
        adjusted_book_balance=adjusted_book,
        difference=difference,
        status=ReconStatus.TIED if difference == ZERO else ReconStatus.UNRECONCILED,
        unexplained_items=unexplained,
    )
