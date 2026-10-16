"""Warehouse interface. Business logic talks to this, never to a specific database."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tieout.models import (
    BankTransaction,
    GLEntry,
    JournalEntryProposal,
    Match,
    ReconException,
    ResolvedFinding,
    RunMetadata,
)


@dataclass(frozen=True)
class CandidatePair:
    """A bank/GL pair produced by a set-based SQL candidate query."""

    bank_id: str
    gl_id: str
    amount: Decimal
    bank_date: date
    gl_date: date
    day_gap: int
    bank_description: str
    gl_description: str
    bank_check: str | None
    gl_check: str | None
    bank_reference: str | None
    gl_reference: str | None


@dataclass(frozen=True)
class LLMCallRecord:
    call_id: str
    run_id: str
    exception_id: str | None
    created_at: datetime
    provider: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: Decimal
    success: bool
    retry_count: int
    error: str | None


@dataclass(frozen=True)
class AgentStepRecord:
    run_id: str
    exception_id: str
    step_no: int
    step_type: str
    tool_name: str | None
    arguments: dict[str, Any] | None
    result: Any
    created_at: datetime


@dataclass(frozen=True)
class ReviewAction:
    action_id: str
    run_id: str
    target_type: str  # EXCEPTION or JE
    target_id: str
    reviewer: str
    decision: str  # APPROVE or REJECT
    note: str
    created_at: datetime


@dataclass(frozen=True)
class RunRow:
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    period: str
    status: str | None
    provider: str
    model: str
    metadata: dict[str, Any]
    result: dict[str, Any] | None


class Warehouse(ABC):
    """Storage port. Implementations: DuckDBWarehouse (default), SnowflakeWarehouse (stub)."""

    @abstractmethod
    def init_schema(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    # runs
    @abstractmethod
    def create_run(self, meta: RunMetadata) -> None: ...

    @abstractmethod
    def finish_run(
        self,
        run_id: str,
        finished_at: datetime,
        status: str,
        metadata: RunMetadata,
        result: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    def update_run_result(self, run_id: str, status: str, result: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> RunRow | None: ...

    @abstractmethod
    def list_runs(self) -> list[RunRow]: ...

    # source data
    @abstractmethod
    def load_bank(self, run_id: str, txns: list[BankTransaction]) -> None: ...

    @abstractmethod
    def load_gl(self, run_id: str, entries: list[GLEntry]) -> None: ...

    @abstractmethod
    def get_bank(self, run_id: str) -> list[BankTransaction]: ...

    @abstractmethod
    def get_gl(self, run_id: str) -> list[GLEntry]: ...

    # matching
    @abstractmethod
    def exact_candidates(self, run_id: str) -> list[CandidatePair]: ...

    @abstractmethod
    def near_candidates(self, run_id: str, window_days: int) -> list[CandidatePair]: ...

    @abstractmethod
    def unmatched_bank(self, run_id: str) -> list[BankTransaction]: ...

    @abstractmethod
    def unmatched_gl(self, run_id: str) -> list[GLEntry]: ...

    @abstractmethod
    def save_matches(self, run_id: str, matches: list[Match]) -> None: ...

    @abstractmethod
    def get_matches(self, run_id: str) -> list[Match]: ...

    # exceptions and findings
    @abstractmethod
    def save_exceptions(self, run_id: str, exceptions: list[ReconException]) -> None: ...

    @abstractmethod
    def get_exceptions(self, run_id: str) -> list[ReconException]: ...

    @abstractmethod
    def save_finding(self, run_id: str, finding: ResolvedFinding) -> None: ...

    @abstractmethod
    def get_findings(self, run_id: str) -> list[ResolvedFinding]: ...

    # agent telemetry
    @abstractmethod
    def log_agent_step(self, step: AgentStepRecord) -> None: ...

    @abstractmethod
    def get_agent_steps(self, run_id: str) -> list[AgentStepRecord]: ...

    @abstractmethod
    def log_llm_call(self, call: LLMCallRecord) -> None: ...

    @abstractmethod
    def get_llm_calls(self, run_id: str | None = None) -> list[LLMCallRecord]: ...

    # journal entries and review
    @abstractmethod
    def save_journal_entries(self, run_id: str, entries: list[JournalEntryProposal]) -> None: ...

    @abstractmethod
    def get_journal_entries(self, run_id: str) -> list[JournalEntryProposal]: ...

    @abstractmethod
    def add_review_action(self, action: ReviewAction) -> None: ...

    @abstractmethod
    def get_review_actions(self, run_id: str) -> list[ReviewAction]: ...
