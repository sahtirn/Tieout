"""Adjusting journal entry proposals for book-side reconciling items.

Amounts come from source records via the schedule arithmetic; accounts come from
config/accounts.yaml. Every proposal is validated as balanced by the model itself.
Bank-side items (deposits in transit, outstanding checks, bank errors) never get a JE: the books
are already right, and the bank will catch up.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from tieout.config import Account, AccountsConfig
from tieout.models import (
    BANK_SIDE_ADJUSTMENTS,
    BreakType,
    GLEntry,
    JELine,
    JournalEntryProposal,
    ResolvedFinding,
    ReviewStatus,
)
from tieout.money import ZERO
from tieout.schedule import adjustment_for

_MEMOS = {
    BreakType.BANK_FEE_UNRECORDED: "Record bank service charge per bank statement",
    BreakType.INTEREST_UNRECORDED: "Record interest earned per bank statement",
    BreakType.NSF_RETURN_UNRECORDED: "Reinstate receivable for customer check returned NSF",
    BreakType.GL_DUPLICATE: "Reverse duplicate GL posting",
    BreakType.GL_TRANSPOSITION: "Correct transposed GL amount to bank-cleared amount",
    BreakType.GL_WRONG_AMOUNT: "Correct GL amount to invoice and bank-cleared amount",
}


def _offset_account(
    finding: ResolvedFinding, accounts: AccountsConfig, gl_by_id: Mapping[str, GLEntry]
) -> Account:
    bt = finding.finding.break_type
    if bt.value in accounts.break_types:
        return accounts.break_types[bt.value]
    gl_id = finding.item_id if finding.side.value == "GL" else finding.finding.counterpart_id
    entry = gl_by_id.get(gl_id or "")
    return accounts.offset_for_journal(entry.journal) if entry else accounts.default_offset


def cash_lines(cash_effect: Decimal, cash: Account, offset: Account) -> list[JELine]:
    """Two balanced lines: cash moves by `cash_effect`, the offset takes the other side."""
    amount = abs(cash_effect)
    if cash_effect > 0:
        return [
            JELine(account=cash.number, account_name=cash.name, debit=amount),
            JELine(account=offset.number, account_name=offset.name, credit=amount),
        ]
    return [
        JELine(account=offset.number, account_name=offset.name, debit=amount),
        JELine(account=cash.number, account_name=cash.name, credit=amount),
    ]


def propose_journal_entries(
    findings: list[ResolvedFinding],
    amounts: Mapping[str, Decimal],
    gl_by_id: Mapping[str, GLEntry],
    accounts: AccountsConfig,
) -> list[JournalEntryProposal]:
    out: list[JournalEntryProposal] = []
    for f in findings:
        bt = f.finding.break_type
        if bt in BANK_SIDE_ADJUSTMENTS or f.review_status == ReviewStatus.REJECTED:
            continue
        effect = adjustment_for(f, amounts)
        if effect == ZERO:
            continue
        offset = _offset_account(f, accounts, gl_by_id)
        items = [f.item_id] + ([f.finding.counterpart_id] if f.finding.counterpart_id else [])
        memo = f"{_MEMOS[bt]} ({', '.join(items)})"
        if f.review_status == ReviewStatus.NEEDS_REVIEW:
            memo += " [classification pending review]"
        out.append(
            JournalEntryProposal(
                je_id=f"AJE-{len(out) + 1:03d}",
                break_type=bt,
                memo=memo,
                source_item_ids=items,
                exception_id=f.exception_id,
                lines=cash_lines(effect, accounts.cash, offset),
            )
        )
    return out
