"""DuckDB implementation of the Warehouse port.

SQL is kept close to ANSI. Money columns are DECIMAL(18,2); LLM cost is DECIMAL(18,6). JSON payloads
are stored as VARCHAR so the schema ports cleanly to engines without a native JSON type.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from tieout.models import (
    BankTransaction,
    GLEntry,
    JournalEntryProposal,
    Match,
    ReconException,
    ResolvedFinding,
    RunMetadata,
)
from tieout.warehouse.base import (
    AgentStepRecord,
    CandidatePair,
    LLMCallRecord,
    ReviewAction,
    RunRow,
    Warehouse,
)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        run_id VARCHAR PRIMARY KEY,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        period VARCHAR NOT NULL,
        status VARCHAR,
        provider VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        metadata_json VARCHAR NOT NULL,
        result_json VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS bank_txns (
        run_id VARCHAR NOT NULL,
        txn_id VARCHAR NOT NULL,
        txn_date DATE NOT NULL,
        description VARCHAR NOT NULL,
        amount DECIMAL(18,2) NOT NULL,
        check_number VARCHAR,
        reference VARCHAR,
        PRIMARY KEY (run_id, txn_id)
    )""",
    """CREATE TABLE IF NOT EXISTS gl_entries (
        run_id VARCHAR NOT NULL,
        entry_id VARCHAR NOT NULL,
        entry_date DATE NOT NULL,
        journal VARCHAR NOT NULL,
        description VARCHAR NOT NULL,
        memo VARCHAR,
        amount DECIMAL(18,2) NOT NULL,
        check_number VARCHAR,
        reference VARCHAR,
        PRIMARY KEY (run_id, entry_id)
    )""",
    """CREATE TABLE IF NOT EXISTS matches (
        run_id VARCHAR NOT NULL,
        match_id VARCHAR NOT NULL,
        rule VARCHAR NOT NULL,
        amount DECIMAL(18,2) NOT NULL,
        confidence DOUBLE NOT NULL,
        evidence_json VARCHAR NOT NULL,
        PRIMARY KEY (run_id, match_id)
    )""",
    # One row per matched item. The primary key makes "matched at most once" a database constraint.
    """CREATE TABLE IF NOT EXISTS match_items (
        run_id VARCHAR NOT NULL,
        match_id VARCHAR NOT NULL,
        side VARCHAR NOT NULL,
        item_id VARCHAR NOT NULL,
        PRIMARY KEY (run_id, side, item_id)
    )""",
    """CREATE TABLE IF NOT EXISTS exceptions (
        run_id VARCHAR NOT NULL,
        exception_id VARCHAR NOT NULL,
        side VARCHAR NOT NULL,
        item_id VARCHAR NOT NULL,
        item_date DATE NOT NULL,
        amount DECIMAL(18,2) NOT NULL,
        description VARCHAR NOT NULL,
        break_type VARCHAR,
        counterpart_id VARCHAR,
        finding_json VARCHAR,
        PRIMARY KEY (run_id, exception_id)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_steps (
        run_id VARCHAR NOT NULL,
        exception_id VARCHAR NOT NULL,
        step_no INTEGER NOT NULL,
        step_type VARCHAR NOT NULL,
        tool_name VARCHAR,
        arguments_json VARCHAR,
        result_json VARCHAR,
        created_at TIMESTAMP NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS llm_calls (
        call_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        exception_id VARCHAR,
        created_at TIMESTAMP NOT NULL,
        provider VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        prompt_version VARCHAR NOT NULL,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        latency_ms INTEGER NOT NULL,
        cost_usd DECIMAL(18,6) NOT NULL,
        success BOOLEAN NOT NULL,
        retry_count INTEGER NOT NULL,
        error VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS journal_entries (
        run_id VARCHAR NOT NULL,
        je_id VARCHAR NOT NULL,
        exception_id VARCHAR NOT NULL,
        break_type VARCHAR NOT NULL,
        total DECIMAL(18,2) NOT NULL,
        je_json VARCHAR NOT NULL,
        PRIMARY KEY (run_id, je_id)
    )""",
    """CREATE TABLE IF NOT EXISTS review_actions (
        action_id VARCHAR PRIMARY KEY,
        run_id VARCHAR NOT NULL,
        target_type VARCHAR NOT NULL,
        target_id VARCHAR NOT NULL,
        reviewer VARCHAR NOT NULL,
        decision VARCHAR NOT NULL,
        note VARCHAR,
        created_at TIMESTAMP NOT NULL
    )""",
]

_CANDIDATE_COLUMNS = """
    b.txn_id, g.entry_id, b.amount, b.txn_date, g.entry_date,
    ABS(b.txn_date - g.entry_date) AS day_gap,
    b.description, g.description, b.check_number, g.check_number, b.reference, g.reference
"""
_UNMATCHED = """
    NOT EXISTS (SELECT 1 FROM match_items mb
                WHERE mb.run_id = b.run_id AND mb.side = 'BANK' AND mb.item_id = b.txn_id)
    AND NOT EXISTS (SELECT 1 FROM match_items mg
                    WHERE mg.run_id = g.run_id AND mg.side = 'GL' AND mg.item_id = g.entry_id)
"""


NULL_SENTINEL = "\\N"  # PostgreSQL-style; a literal backslash-N value would load as NULL


def _csv_value(v: Any) -> str:
    if v is None:
        return NULL_SENTINEL
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


class DuckDBWarehouse(Warehouse):
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.con = duckdb.connect(path)
        self.init_schema()

    def init_schema(self) -> None:
        for ddl in SCHEMA:
            self.con.execute(ddl)

    def close(self) -> None:
        self.con.close()

    def _bulk_insert(self, table: str, rows: list[list[Any]]) -> None:
        """Bulk load through a temporary CSV and COPY.

        DuckDB's Python parameter binding probes for optional libraries on every bound value,
        which costs roughly 250 microseconds per value. Staging to CSV and using COPY is about
        100x faster and mirrors how a Snowflake port would stage files and COPY INTO.
        Every value is written as a quoted string, so Decimal precision is preserved exactly.
        """
        if not rows:
            return
        fd, name = tempfile.mkstemp(prefix="tieout-", suffix=".csv")
        path = Path(name)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh, lineterminator="\n", quoting=csv.QUOTE_NONNUMERIC)
                for row in rows:
                    writer.writerow([_csv_value(v) for v in row])
            literal = str(path).replace("'", "''")
            self.con.execute(
                f"COPY {table} FROM '{literal}' (FORMAT csv, AUTO_DETECT false, HEADER false, "
                "DELIM ',', QUOTE '\"', ESCAPE '\"', NULLSTR '\\N')"
            )
        finally:
            path.unlink(missing_ok=True)

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, params or []).fetchall()

    # ----- runs --------------------------------------------------------------------------------
    def create_run(self, meta: RunMetadata) -> None:
        self.con.execute(
            "INSERT INTO runs (run_id, started_at, period, provider, model, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                meta.run_id,
                meta.started_at,
                meta.period,
                meta.provider,
                meta.model,
                meta.model_dump_json(),
            ],
        )

    def finish_run(
        self,
        run_id: str,
        finished_at: datetime,
        status: str,
        metadata: RunMetadata,
        result: dict[str, Any],
    ) -> None:
        self.con.execute(
            "UPDATE runs SET finished_at = ?, status = ?, metadata_json = ?, result_json = ? "
            "WHERE run_id = ?",
            [finished_at, status, metadata.model_dump_json(), _dumps(result), run_id],
        )

    def update_run_result(self, run_id: str, status: str, result: dict[str, Any]) -> None:
        self.con.execute(
            "UPDATE runs SET status = ?, result_json = ? WHERE run_id = ?",
            [status, _dumps(result), run_id],
        )

    def _run_row(self, r: tuple[Any, ...]) -> RunRow:
        return RunRow(
            run_id=r[0],
            started_at=r[1],
            finished_at=r[2],
            period=r[3],
            status=r[4],
            provider=r[5],
            model=r[6],
            metadata=json.loads(r[7]),
            result=json.loads(r[8]) if r[8] else None,
        )

    def get_run(self, run_id: str) -> RunRow | None:
        rows = self._rows(
            "SELECT run_id, started_at, finished_at, period, status, provider, model, "
            "metadata_json, result_json FROM runs WHERE run_id = ?",
            [run_id],
        )
        return self._run_row(rows[0]) if rows else None

    def list_runs(self) -> list[RunRow]:
        rows = self._rows(
            "SELECT run_id, started_at, finished_at, period, status, provider, model, "
            "metadata_json, result_json FROM runs ORDER BY started_at, run_id"
        )
        return [self._run_row(r) for r in rows]

    # ----- source data -------------------------------------------------------------------------
    def load_bank(self, run_id: str, txns: list[BankTransaction]) -> None:
        self._bulk_insert(
            "bank_txns",
            [
                [run_id, t.txn_id, t.txn_date, t.description, t.amount, t.check_number, t.reference]
                for t in txns
            ],
        )

    def load_gl(self, run_id: str, entries: list[GLEntry]) -> None:
        self._bulk_insert(
            "gl_entries",
            [
                [
                    run_id,
                    e.entry_id,
                    e.entry_date,
                    e.journal,
                    e.description,
                    e.memo,
                    e.amount,
                    e.check_number,
                    e.reference,
                ]
                for e in entries
            ],
        )

    @staticmethod
    def _bank(r: tuple[Any, ...]) -> BankTransaction:
        return BankTransaction(
            txn_id=r[0],
            txn_date=r[1],
            description=r[2],
            amount=r[3],
            check_number=r[4],
            reference=r[5],
        )

    @staticmethod
    def _gl(r: tuple[Any, ...]) -> GLEntry:
        return GLEntry(
            entry_id=r[0],
            entry_date=r[1],
            journal=r[2],
            description=r[3],
            memo=r[4] or "",
            amount=r[5],
            check_number=r[6],
            reference=r[7],
        )

    def get_bank(self, run_id: str) -> list[BankTransaction]:
        rows = self._rows(
            "SELECT txn_id, txn_date, description, amount, check_number, reference "
            "FROM bank_txns WHERE run_id = ? ORDER BY txn_id",
            [run_id],
        )
        return [self._bank(r) for r in rows]

    def get_gl(self, run_id: str) -> list[GLEntry]:
        rows = self._rows(
            "SELECT entry_id, entry_date, journal, description, memo, amount, check_number, "
            "reference FROM gl_entries WHERE run_id = ? ORDER BY entry_id",
            [run_id],
        )
        return [self._gl(r) for r in rows]

    # ----- matching ----------------------------------------------------------------------------
    @staticmethod
    def _pair(r: tuple[Any, ...]) -> CandidatePair:
        return CandidatePair(
            bank_id=r[0],
            gl_id=r[1],
            amount=r[2],
            bank_date=r[3],
            gl_date=r[4],
            day_gap=int(r[5]),
            bank_description=r[6],
            gl_description=r[7],
            bank_check=r[8],
            gl_check=r[9],
            bank_reference=r[10],
            gl_reference=r[11],
        )

    def exact_candidates(self, run_id: str) -> list[CandidatePair]:
        sql = f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM bank_txns b
            JOIN gl_entries g
              ON g.run_id = b.run_id AND g.amount = b.amount AND g.entry_date = b.txn_date
            WHERE b.run_id = ?
              AND ((b.check_number IS NOT NULL AND b.check_number = g.check_number)
                   OR (b.reference IS NOT NULL AND b.reference = g.reference))
              AND {_UNMATCHED}
            ORDER BY b.txn_id, g.entry_id
        """
        return [self._pair(r) for r in self._rows(sql, [run_id])]

    def near_candidates(self, run_id: str, window_days: int) -> list[CandidatePair]:
        sql = f"""
            SELECT {_CANDIDATE_COLUMNS}
            FROM bank_txns b
            JOIN gl_entries g ON g.run_id = b.run_id AND g.amount = b.amount
            WHERE b.run_id = ?
              AND ABS(b.txn_date - g.entry_date) <= ?
              AND {_UNMATCHED}
            ORDER BY b.txn_id, g.entry_id
        """
        return [self._pair(r) for r in self._rows(sql, [run_id, window_days])]

    def unmatched_bank(self, run_id: str) -> list[BankTransaction]:
        rows = self._rows(
            "SELECT txn_id, txn_date, description, amount, check_number, reference "
            "FROM bank_txns b WHERE run_id = ? AND NOT EXISTS (SELECT 1 FROM match_items m "
            "WHERE m.run_id = b.run_id AND m.side = 'BANK' AND m.item_id = b.txn_id) "
            "ORDER BY txn_date, txn_id",
            [run_id],
        )
        return [self._bank(r) for r in rows]

    def unmatched_gl(self, run_id: str) -> list[GLEntry]:
        rows = self._rows(
            "SELECT entry_id, entry_date, journal, description, memo, amount, check_number, "
            "reference FROM gl_entries g WHERE run_id = ? AND NOT EXISTS (SELECT 1 FROM "
            "match_items m WHERE m.run_id = g.run_id AND m.side = 'GL' AND m.item_id = g.entry_id) "
            "ORDER BY entry_date, entry_id",
            [run_id],
        )
        return [self._gl(r) for r in rows]

    def save_matches(self, run_id: str, matches: list[Match]) -> None:
        if not matches:
            return
        self.con.execute("BEGIN TRANSACTION")
        try:
            self._bulk_insert(
                "matches",
                [
                    [run_id, m.match_id, m.rule.value, m.amount, m.confidence, _dumps(m.evidence)]
                    for m in matches
                ],
            )
            items = [[run_id, m.match_id, "BANK", i] for m in matches for i in m.bank_ids]
            items += [[run_id, m.match_id, "GL", i] for m in matches for i in m.gl_ids]
            self._bulk_insert("match_items", items)
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def get_matches(self, run_id: str) -> list[Match]:
        items: dict[str, dict[str, list[str]]] = {}
        for match_id, side, item_id in self._rows(
            "SELECT match_id, side, item_id FROM match_items WHERE run_id = ? "
            "ORDER BY match_id, side, item_id",
            [run_id],
        ):
            items.setdefault(match_id, {"BANK": [], "GL": []})[side].append(item_id)
        out = []
        for match_id, rule, amount, confidence, evidence in self._rows(
            "SELECT match_id, rule, amount, confidence, evidence_json FROM matches "
            "WHERE run_id = ? ORDER BY match_id",
            [run_id],
        ):
            out.append(
                Match(
                    match_id=match_id,
                    rule=rule,
                    amount=amount,
                    confidence=confidence,
                    evidence=json.loads(evidence),
                    bank_ids=items[match_id]["BANK"],
                    gl_ids=items[match_id]["GL"],
                )
            )
        return out

    # ----- exceptions and findings -------------------------------------------------------------
    def save_exceptions(self, run_id: str, exceptions: list[ReconException]) -> None:
        if not exceptions:
            return
        self._bulk_insert(
            "exceptions",
            [
                [
                    run_id,
                    e.exception_id,
                    e.side.value,
                    e.item_id,
                    e.item_date,
                    e.amount,
                    e.description,
                    None,
                    None,
                    None,
                ]
                for e in exceptions
            ],
        )

    def get_exceptions(self, run_id: str) -> list[ReconException]:
        rows = self._rows(
            "SELECT exception_id, side, item_id, item_date, amount, description FROM exceptions "
            "WHERE run_id = ? ORDER BY exception_id",
            [run_id],
        )
        return [
            ReconException(
                exception_id=r[0],
                side=r[1],
                item_id=r[2],
                item_date=r[3],
                amount=r[4],
                description=r[5],
            )
            for r in rows
        ]

    def save_finding(self, run_id: str, finding: ResolvedFinding) -> None:
        self.con.execute(
            "UPDATE exceptions SET break_type = ?, counterpart_id = ?, finding_json = ? "
            "WHERE run_id = ? AND exception_id = ?",
            [
                finding.finding.break_type.value,
                finding.finding.counterpart_id,
                finding.model_dump_json(),
                run_id,
                finding.exception_id,
            ],
        )

    def get_findings(self, run_id: str) -> list[ResolvedFinding]:
        rows = self._rows(
            "SELECT finding_json FROM exceptions WHERE run_id = ? AND finding_json IS NOT NULL "
            "ORDER BY exception_id",
            [run_id],
        )
        return [ResolvedFinding.model_validate_json(r[0]) for r in rows]

    # ----- telemetry ---------------------------------------------------------------------------
    def log_agent_step(self, step: AgentStepRecord) -> None:
        self.con.execute(
            "INSERT INTO agent_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                step.run_id,
                step.exception_id,
                step.step_no,
                step.step_type,
                step.tool_name,
                _dumps(step.arguments) if step.arguments is not None else None,
                _dumps(step.result) if step.result is not None else None,
                step.created_at,
            ],
        )

    def get_agent_steps(self, run_id: str) -> list[AgentStepRecord]:
        rows = self._rows(
            "SELECT run_id, exception_id, step_no, step_type, tool_name, arguments_json, "
            "result_json, created_at FROM agent_steps WHERE run_id = ? "
            "ORDER BY exception_id, step_no",
            [run_id],
        )
        return [
            AgentStepRecord(
                run_id=r[0],
                exception_id=r[1],
                step_no=r[2],
                step_type=r[3],
                tool_name=r[4],
                arguments=json.loads(r[5]) if r[5] else None,
                result=json.loads(r[6]) if r[6] else None,
                created_at=r[7],
            )
            for r in rows
        ]

    def log_llm_call(self, call: LLMCallRecord) -> None:
        self.con.execute(
            "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                call.call_id,
                call.run_id,
                call.exception_id,
                call.created_at,
                call.provider,
                call.model,
                call.prompt_version,
                call.input_tokens,
                call.output_tokens,
                call.latency_ms,
                call.cost_usd,
                call.success,
                call.retry_count,
                call.error,
            ],
        )

    def get_llm_calls(self, run_id: str | None = None) -> list[LLMCallRecord]:
        sql = (
            "SELECT call_id, run_id, exception_id, created_at, provider, model, prompt_version, "
            "input_tokens, output_tokens, latency_ms, cost_usd, success, retry_count, error "
            "FROM llm_calls"
        )
        params: list[Any] = []
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params.append(run_id)
        sql += " ORDER BY created_at, call_id"
        return [LLMCallRecord(*r) for r in self._rows(sql, params)]

    # ----- journal entries and review ----------------------------------------------------------
    def save_journal_entries(self, run_id: str, entries: list[JournalEntryProposal]) -> None:
        if not entries:
            return
        self._bulk_insert(
            "journal_entries",
            [
                [
                    run_id,
                    je.je_id,
                    je.exception_id,
                    je.break_type.value,
                    je.total,
                    je.model_dump_json(),
                ]
                for je in entries
            ],
        )

    def get_journal_entries(self, run_id: str) -> list[JournalEntryProposal]:
        rows = self._rows(
            "SELECT je_json FROM journal_entries WHERE run_id = ? ORDER BY je_id", [run_id]
        )
        return [JournalEntryProposal.model_validate_json(r[0]) for r in rows]

    def add_review_action(self, action: ReviewAction) -> None:
        self.con.execute(
            "INSERT INTO review_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                action.action_id,
                action.run_id,
                action.target_type,
                action.target_id,
                action.reviewer,
                action.decision,
                action.note,
                action.created_at,
            ],
        )

    def get_review_actions(self, run_id: str) -> list[ReviewAction]:
        rows = self._rows(
            "SELECT action_id, run_id, target_type, target_id, reviewer, decision, note, "
            "created_at FROM review_actions WHERE run_id = ? ORDER BY created_at, action_id",
            [run_id],
        )
        return [ReviewAction(*r) for r in rows]
