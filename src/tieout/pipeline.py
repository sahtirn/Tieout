"""End-to-end reconciliation orchestration."""

from __future__ import annotations

import calendar
import subprocess
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tieout.agent.investigator import Investigator
from tieout.agent.providers import LLMProvider, make_provider
from tieout.agent.tools import LedgerView
from tieout.config import PROJECT_ROOT, Settings, load_accounts, load_assumptions, load_pricing
from tieout.io import load_bank_csv, load_gl_csv, sha256_file
from tieout.journal import propose_journal_entries
from tieout.matching import MatchingEngine, exceptions_from_unmatched
from tieout.models import (
    InputFile,
    ReviewStatus,
    RunMetadata,
)
from tieout.review import apply_reviews
from tieout.schedule import build_tieout
from tieout.telemetry import estimate_roi, summarize
from tieout.warehouse.base import Warehouse


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def period_bounds(period: str) -> tuple[date, date]:
    year, month = (int(x) for x in period.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def reconcile(
    bank_path: Path,
    gl_path: Path,
    settings: Settings,
    warehouse: Warehouse,
    provider: LLMProvider | None = None,
    preparer: str = "tieout-agent",
    period: str | None = None,
) -> dict[str, Any]:
    """Run matching, investigation, tie-out, and JE proposals. Returns the result document."""
    started = _now()
    bank = load_bank_csv(bank_path)
    gl = load_gl_csv(gl_path)
    if period is None:
        last = max(t.txn_date for t in bank.transactions)
        period = f"{last.year:04d}-{last.month:02d}"
    p_start, p_end = period_bounds(period)
    provider = provider or make_provider(settings)
    meta = RunMetadata(
        run_id=str(uuid.uuid4()),
        started_at=started,
        period=period,
        entity=settings.entity_name,
        account=settings.account_name,
        inputs=[
            InputFile(
                role="bank_statement",
                filename=bank_path.name,
                sha256=sha256_file(bank_path),
                rows=len(bank.transactions),
            ),
            InputFile(
                role="gl_cash",
                filename=gl_path.name,
                sha256=sha256_file(gl_path),
                rows=len(gl.entries),
            ),
        ],
        config_snapshot=settings.snapshot(),
        git_commit=git_commit(),
        provider=provider.name,
        model=provider.model,
        prompt_version=settings.prompt_version,
        preparer=preparer,
        bank_opening_balance=bank.opening_balance,
        bank_ending_balance=bank.ending_balance,
        gl_opening_balance=gl.opening_balance,
        gl_ending_balance=gl.ending_balance,
    )
    run_id = meta.run_id
    warehouse.create_run(meta)
    warehouse.load_bank(run_id, bank.transactions)
    warehouse.load_gl(run_id, gl.entries)

    matches, stats = MatchingEngine(warehouse, settings).run(run_id)
    exceptions = exceptions_from_unmatched(
        warehouse.unmatched_bank(run_id), warehouse.unmatched_gl(run_id)
    )
    warehouse.save_exceptions(run_id, exceptions)

    matched: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for m in matches:
        members[m.match_id] = [*m.bank_ids, *m.gl_ids]
        for i in members[m.match_id]:
            matched[i] = m.match_id
    view = LedgerView(
        bank={t.txn_id: t for t in bank.transactions},
        gl={e.entry_id: e for e in gl.entries},
        matched=matched,
        match_members=members,
        period_start=p_start,
        period_end=p_end,
        window_days=settings.date_window_days,
    )
    investigator = Investigator(
        warehouse, settings, provider, view, load_pricing(settings.config_dir), run_id
    )
    findings = investigator.investigate_all(exceptions)

    amounts: dict[str, Decimal] = {t.txn_id: t.amount for t in bank.transactions}
    amounts.update({e.entry_id: e.amount for e in gl.entries})
    jes = propose_journal_entries(findings, amounts, view.gl, load_accounts(settings.config_dir))
    warehouse.save_journal_entries(run_id, jes)

    result = build_result(warehouse, run_id, settings, match_stats=stats.as_dict())
    finished = meta.model_copy(update={"finished_at": _now()})
    result["metadata"] = finished.model_dump(mode="json")
    warehouse.finish_run(run_id, finished.finished_at or _now(), result["status"], finished, result)
    return result


def build_result(
    warehouse: Warehouse,
    run_id: str,
    settings: Settings,
    match_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the result document from stored state, applying any review actions."""
    run = warehouse.get_run(run_id)
    if run is None:
        raise KeyError(f"run {run_id} not found")
    meta = RunMetadata.model_validate(run.metadata)
    if match_stats is None and run.result is not None:
        match_stats = run.result.get("match_stats", {})
    bank = warehouse.get_bank(run_id)
    gl = warehouse.get_gl(run_id)
    matches = warehouse.get_matches(run_id)
    exceptions = warehouse.get_exceptions(run_id)
    actions = warehouse.get_review_actions(run_id)
    findings, jes = apply_reviews(
        warehouse.get_findings(run_id), warehouse.get_journal_entries(run_id), actions
    )
    amounts: dict[str, Decimal] = {t.txn_id: t.amount for t in bank}
    amounts.update({e.entry_id: e.amount for e in gl})
    tie = build_tieout(
        meta.period, meta.bank_ending_balance, meta.gl_ending_balance, exceptions, findings, amounts
    )
    calls = warehouse.get_llm_calls(run_id)
    tel = summarize(calls)
    by_status = {s.value: sum(1 for f in findings if f.review_status == s) for s in ReviewStatus}
    matched_items = sum(len(m.bank_ids) + len(m.gl_ids) for m in matches)
    roi = estimate_roi(
        load_assumptions(settings.config_dir),
        matched_items,
        by_status[ReviewStatus.AUTO_ACCEPTED.value],
        tel.cost_usd,
    )
    rules: dict[str, int] = {}
    for m in matches:
        rules[m.rule.value] = rules.get(m.rule.value, 0) + 1
    return {
        "run_id": run_id,
        "status": tie.status.value,
        "period": meta.period,
        "entity": meta.entity,
        "account": meta.account,
        "summary": {
            "bank_lines": len(bank),
            "gl_lines": len(gl),
            "matches": len(matches),
            "matches_by_rule": rules,
            "matched_items": matched_items,
            "exceptions": len(exceptions),
            "findings": len(findings),
            "findings_by_status": by_status,
            "journal_entries": len(jes),
            "difference": format(tie.difference, "f"),
        },
        "tieout": tie.model_dump(mode="json"),
        "match_stats": match_stats or {},
        "matches": [m.model_dump(mode="json") for m in matches],
        "exceptions": [e.model_dump(mode="json") for e in exceptions],
        "findings": [f.model_dump(mode="json") for f in findings],
        "journal_entries": [je.model_dump(mode="json") for je in jes],
        "review_actions": [
            {
                "action_id": a.action_id,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "reviewer": a.reviewer,
                "decision": a.decision,
                "note": a.note,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ],
        "telemetry": tel.as_dict(),
        "roi": roi.as_dict(),
        "metadata": meta.model_dump(mode="json"),
    }
