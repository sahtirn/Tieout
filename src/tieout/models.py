"""Domain models.

Sign convention: every amount is signed from the cash account's perspective.
Deposits and debits to cash are positive; withdrawals and credits to cash are negative.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tieout.money import ZERO, Money, to_money


class Side(StrEnum):
    BANK = "BANK"
    GL = "GL"


class MatchRule(StrEnum):
    EXACT = "EXACT"
    NEAR = "NEAR"
    MANY_TO_ONE = "MANY_TO_ONE"


class BreakType(StrEnum):
    DEPOSIT_IN_TRANSIT = "DEPOSIT_IN_TRANSIT"
    OUTSTANDING_CHECK = "OUTSTANDING_CHECK"
    BANK_FEE_UNRECORDED = "BANK_FEE_UNRECORDED"
    INTEREST_UNRECORDED = "INTEREST_UNRECORDED"
    NSF_RETURN_UNRECORDED = "NSF_RETURN_UNRECORDED"
    GL_DUPLICATE = "GL_DUPLICATE"
    GL_TRANSPOSITION = "GL_TRANSPOSITION"
    GL_WRONG_AMOUNT = "GL_WRONG_AMOUNT"
    BANK_ERROR = "BANK_ERROR"
    # Honest "cannot determine". Never counted on the schedule; always routed to a reviewer.
    UNIDENTIFIED = "UNIDENTIFIED"


# Which ledger an item must live on for a given break type, and the required sign of its amount.
BREAK_SIDE: dict[BreakType, Side | None] = {
    BreakType.DEPOSIT_IN_TRANSIT: Side.GL,
    BreakType.OUTSTANDING_CHECK: Side.GL,
    BreakType.GL_DUPLICATE: Side.GL,
    BreakType.BANK_FEE_UNRECORDED: Side.BANK,
    BreakType.INTEREST_UNRECORDED: Side.BANK,
    BreakType.NSF_RETURN_UNRECORDED: Side.BANK,
    BreakType.BANK_ERROR: Side.BANK,
    BreakType.GL_TRANSPOSITION: None,  # either side; requires a counterpart on the other side
    BreakType.GL_WRONG_AMOUNT: None,
    BreakType.UNIDENTIFIED: None,
}
BREAK_SIGN: dict[BreakType, int] = {
    BreakType.DEPOSIT_IN_TRANSIT: 1,
    BreakType.OUTSTANDING_CHECK: -1,
    BreakType.BANK_FEE_UNRECORDED: -1,
    BreakType.INTEREST_UNRECORDED: 1,
    BreakType.NSF_RETURN_UNRECORDED: -1,
}
PAIRED_BREAKS = frozenset({BreakType.GL_TRANSPOSITION, BreakType.GL_WRONG_AMOUNT})
BANK_SIDE_ADJUSTMENTS = frozenset(
    {BreakType.DEPOSIT_IN_TRANSIT, BreakType.OUTSTANDING_CHECK, BreakType.BANK_ERROR}
)


class ReviewStatus(StrEnum):
    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FindingSource(StrEnum):
    AGENT = "AGENT"
    FALLBACK = "FALLBACK"


class ReconStatus(StrEnum):
    TIED = "TIED"
    UNRECONCILED = "UNRECONCILED"


class JEStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BankTransaction(_Frozen):
    txn_id: str
    txn_date: date
    description: str
    amount: Money
    check_number: str | None = None
    reference: str | None = None

    @property
    def side(self) -> Side:
        return Side.BANK

    @property
    def item_id(self) -> str:
        return self.txn_id

    @property
    def item_date(self) -> date:
        return self.txn_date


class GLEntry(_Frozen):
    entry_id: str
    entry_date: date
    journal: str
    description: str
    memo: str = ""
    amount: Money
    check_number: str | None = None
    reference: str | None = None

    @property
    def side(self) -> Side:
        return Side.GL

    @property
    def item_id(self) -> str:
        return self.entry_id

    @property
    def item_date(self) -> date:
        return self.entry_date


LedgerItem = BankTransaction | GLEntry


class Match(_Frozen):
    match_id: str
    rule: MatchRule
    bank_ids: list[str]
    gl_ids: list[str]
    amount: Money
    confidence: float = Field(ge=0, le=1)
    evidence: dict[str, Any]

    @model_validator(mode="after")
    def _non_empty(self) -> Match:
        if not self.bank_ids or not self.gl_ids:
            raise ValueError("a match needs at least one bank and one GL item")
        return self


class ReconException(_Frozen):
    exception_id: str
    side: Side
    item_id: str
    item_date: date
    amount: Money
    description: str


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def count_sentences(text: str) -> int:
    parts = [p for p in _SENTENCE_SPLIT.split(text.strip()) if p.strip()]
    return len(parts)


class AgentFinding(_Frozen):
    """The only structured output the LLM is allowed to produce."""

    break_type: BreakType
    explanation: str = Field(min_length=20, max_length=1200)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    proposed_action: str = Field(min_length=5, max_length=400)
    confidence: float = Field(ge=0, le=1)
    counterpart_id: str | None = None

    @field_validator("explanation")
    @classmethod
    def _two_to_four_sentences(cls, v: str) -> str:
        n = count_sentences(v)
        if not 2 <= n <= 4:
            raise ValueError(f"explanation must be 2 to 4 sentences, got {n}")
        return v


class ResolvedFinding(_Frozen):
    """A validated classification for one exception, with provenance and review state."""

    exception_id: str
    item_id: str
    side: Side
    finding: AgentFinding
    source: FindingSource
    review_status: ReviewStatus
    review_reasons: list[str] = Field(default_factory=list)
    prompt_version: str
    steps_used: int = 0

    @property
    def counted_in_schedule(self) -> bool:
        return self.review_status in (ReviewStatus.AUTO_ACCEPTED, ReviewStatus.APPROVED)


class JELine(_Frozen):
    account: str
    account_name: str
    debit: Money = ZERO
    credit: Money = ZERO

    @model_validator(mode="after")
    def _one_side(self) -> JELine:
        if self.debit < 0 or self.credit < 0:
            raise ValueError("debits and credits must be non-negative")
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("each JE line must have exactly one of debit or credit")
        return self


class JournalEntryProposal(_Frozen):
    je_id: str
    break_type: BreakType
    memo: str
    source_item_ids: list[str]
    exception_id: str
    lines: list[JELine] = Field(min_length=2)
    status: JEStatus = JEStatus.PENDING_APPROVAL

    @model_validator(mode="after")
    def _balanced(self) -> JournalEntryProposal:
        debits = sum((ln.debit for ln in self.lines), ZERO)
        credits = sum((ln.credit for ln in self.lines), ZERO)
        if to_money(debits) != to_money(credits):
            raise ValueError(f"unbalanced journal entry: debits {debits} != credits {credits}")
        return self

    @property
    def total(self) -> Decimal:
        return to_money(sum((ln.debit for ln in self.lines), ZERO))


class ScheduleLine(_Frozen):
    exception_id: str
    break_type: BreakType
    item_ids: list[str]
    description: str
    amount: Money  # signed effect on the adjusted balance of its side
    review_status: ReviewStatus


class TieOut(_Frozen):
    period: str
    bank_ending_balance: Money
    gl_ending_balance: Money
    deposits_in_transit: list[ScheduleLine]
    outstanding_checks: list[ScheduleLine]
    bank_errors: list[ScheduleLine]
    unrecorded_credits: list[ScheduleLine]
    unrecorded_debits: list[ScheduleLine]
    book_errors: list[ScheduleLine]
    adjusted_bank_balance: Money
    adjusted_book_balance: Money
    difference: Money
    status: ReconStatus
    unexplained_items: list[ReconException]

    @staticmethod
    def total(lines: list[ScheduleLine]) -> Decimal:
        return to_money(sum((ln.amount for ln in lines), ZERO))


class InputFile(_Frozen):
    role: str
    filename: str
    sha256: str
    rows: int


class RunMetadata(_Frozen):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    period: str
    entity: str
    account: str
    inputs: list[InputFile]
    config_snapshot: dict[str, Any]
    git_commit: str | None = None
    provider: str
    model: str
    prompt_version: str
    preparer: str
    bank_opening_balance: Money
    bank_ending_balance: Money
    gl_opening_balance: Money
    gl_ending_balance: Money
