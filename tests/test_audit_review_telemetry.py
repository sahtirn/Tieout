from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tieout.agent.providers import MockProvider
from tieout.agent.providers.base import Message, ProviderResponse
from tieout.audit import export_audit
from tieout.config import AssumptionsConfig, Settings, load_pricing
from tieout.pipeline import build_result, reconcile
from tieout.review import ReviewError, pending_items, record_review
from tieout.telemetry import estimate_cost, estimate_roi, summarize, telemetry_report
from tieout.warehouse import DuckDBWarehouse


class UnsureMock(MockProvider):
    """Mock policy that reports low confidence, so every finding needs human review."""

    def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        resp = super().complete(system, messages, tools)
        if resp.tool_call and resp.tool_call.name == "submit_finding":
            resp.tool_call.args["confidence"] = 0.5
        return resp


def run(dataset_dir: Path, provider: Any = None) -> tuple[DuckDBWarehouse, dict[str, Any]]:
    wh = DuckDBWarehouse()
    result = reconcile(
        dataset_dir / "bank_statement.csv",
        dataset_dir / "gl_cash.csv",
        Settings(),
        wh,
        provider=provider,
        preparer="pat.preparer",
    )
    return wh, result


def test_audit_trail_traces_every_reconciling_item(dataset_dir: Path, tmp_path: Path) -> None:
    wh, result = run(dataset_dir)
    out = tmp_path / "audit.jsonl"
    n = export_audit(wh, result["run_id"], Settings(), out)
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(records) == n
    assert records[0]["record"] == "run" and records[-1]["record"] == "tieout"
    kinds = {r["record"] for r in records}
    assert {
        "source_row",
        "match",
        "exception",
        "agent_step",
        "llm_call",
        "finding",
        "journal_entry",
        "reconciling_item_trace",
    } <= kinds
    traces = [r for r in records if r["record"] == "reconciling_item_trace"]
    assert len(traces) == len(result["findings"])
    for t in traces:
        assert t["source_rows"] and t["source_rows"][0]["ledger"] in ("BANK", "GL")
        assert t["agent_steps"][-1]["step_type"] == "final"
        assert not t["schedule"]["section"].startswith("UNEXPLAINED")
        if t["schedule"]["section"].startswith("BOOK"):
            assert t["journal_entry_ids"], t
    rows = [r for r in records if r["record"] == "source_row"]
    assert len(rows) == result["summary"]["bank_lines"] + result["summary"]["gl_lines"]


def test_maker_checker_controls(dataset_dir: Path) -> None:
    wh, result = run(dataset_dir)
    rid = result["run_id"]
    exc_id = result["findings"][0]["exception_id"]
    with pytest.raises(ReviewError, match="maker-checker"):
        record_review(wh, rid, "EXCEPTION", exc_id, "Pat.Preparer", "APPROVE", "")
    with pytest.raises(ReviewError, match="requires a note"):
        record_review(wh, rid, "EXCEPTION", exc_id, "riley.reviewer", "REJECT", " ")
    with pytest.raises(ReviewError, match="not found"):
        record_review(wh, "nope", "EXCEPTION", exc_id, "riley.reviewer", "APPROVE", "")
    with pytest.raises(ReviewError, match="no finding"):
        record_review(wh, rid, "EXCEPTION", "EXC-9999", "riley.reviewer", "APPROVE", "")
    with pytest.raises(ReviewError, match="not found"):
        record_review(wh, rid, "JE", "AJE-999", "riley.reviewer", "APPROVE", "")
    with pytest.raises(ReviewError, match="target type"):
        record_review(wh, rid, "MATCH", "M-1", "riley.reviewer", "APPROVE", "")
    with pytest.raises(ReviewError, match="decision"):
        record_review(wh, rid, "JE", "AJE-001", "riley.reviewer", "MAYBE", "")
    with pytest.raises(ReviewError, match="reviewer name"):
        record_review(wh, rid, "JE", "AJE-001", "  ", "APPROVE", "")


def test_review_changes_tie_out_and_je_status(dataset_dir: Path) -> None:
    wh, result = run(dataset_dir)
    rid = result["run_id"]
    fee = next(f for f in result["findings"] if f["finding"]["break_type"] == "BANK_FEE_UNRECORDED")
    je = next(j for j in result["journal_entries"] if j["exception_id"] == fee["exception_id"])
    record_review(wh, rid, "JE", je["je_id"], "riley.reviewer", "APPROVE", "agrees to statement")
    after = build_result(wh, rid, Settings())
    assert (
        next(j for j in after["journal_entries"] if j["je_id"] == je["je_id"])["status"]
        == "APPROVED"
    )
    record_review(
        wh, rid, "EXCEPTION", fee["exception_id"], "riley.reviewer", "REJECT", "fee was refunded"
    )
    after = build_result(wh, rid, Settings())
    assert after["status"] == "UNRECONCILED"
    assert after["tieout"]["unexplained_items"][0]["item_id"] == fee["item_id"]
    record_review(
        wh,
        rid,
        "EXCEPTION",
        fee["exception_id"],
        "riley.reviewer",
        "APPROVE",
        "refund was a different item",
    )
    assert build_result(wh, rid, Settings())["status"] == "TIED"
    assert len(build_result(wh, rid, Settings())["review_actions"]) == 3


def test_human_approval_closes_needs_review_gap(dataset_dir: Path) -> None:
    wh, result = run(dataset_dir, provider=UnsureMock())
    rid = result["run_id"]
    assert result["status"] == "UNRECONCILED"
    assert result["summary"]["findings_by_status"]["NEEDS_REVIEW"] == len(result["findings"])
    assert all("[classification pending review]" in je["memo"] for je in result["journal_entries"])
    from tieout.models import JournalEntryProposal, ResolvedFinding

    findings, jes = pending_items(
        [ResolvedFinding.model_validate(f) for f in result["findings"]],
        [JournalEntryProposal.model_validate(j) for j in result["journal_entries"]],
    )
    assert len(findings) == len(result["findings"]) and len(jes) == len(result["journal_entries"])
    for f in findings:
        record_review(
            wh, rid, "EXCEPTION", f.exception_id, "riley.reviewer", "APPROVE", "evidence verified"
        )
    after = build_result(wh, rid, Settings())
    assert after["status"] == "TIED"
    assert after["summary"]["findings_by_status"]["APPROVED"] == len(findings)


def test_unidentified_cannot_be_approved(dataset_dir: Path) -> None:
    wh, result = run(dataset_dir)
    from tieout.models import (
        AgentFinding,
        BreakType,
        FindingSource,
        ResolvedFinding,
        ReviewStatus,
    )

    f = ResolvedFinding.model_validate(result["findings"][0])
    unknown = f.model_copy(
        update={
            "finding": AgentFinding(
                break_type=BreakType.UNIDENTIFIED,
                explanation="Could not tell. Needs a human.",
                evidence_ids=[f.item_id],
                proposed_action="Investigate manually",
                confidence=0.0,
            ),
            "source": FindingSource.FALLBACK,
            "review_status": ReviewStatus.NEEDS_REVIEW,
        }
    )
    wh.save_finding(result["run_id"], unknown)
    with pytest.raises(ReviewError, match="UNIDENTIFIED"):
        record_review(wh, result["run_id"], "EXCEPTION", f.exception_id, "riley", "APPROVE", "")


def test_cost_and_roi_math() -> None:
    pricing = load_pricing()
    assert estimate_cost(pricing, "gemini-3.6-flash", 1_000_000, 0) == Decimal("0.500000")
    assert estimate_cost(pricing, "gemini-3.6-flash", 1000, 500) == Decimal("0.002000")
    assert estimate_cost(pricing, "mock", 10**6, 10**6) == Decimal("0")
    assert estimate_cost(pricing, "unknown-model", 1_000_000, 0) == Decimal("0.500000")
    roi = estimate_roi(
        AssumptionsConfig(
            minutes_per_matched_item=Decimal("0.5"),
            minutes_per_exception=Decimal("12"),
            analyst_hourly_rate=Decimal("60"),
        ),
        100,
        5,
        Decimal("0.25"),
    )
    assert roi.minutes_saved == Decimal("110.0")
    assert roi.value_usd == Decimal("110.00")
    assert roi.net_usd == Decimal("109.75")
    assert "ASSUMPTION" in next(iter(roi.as_dict()))
    assert summarize([]).failure_rate == 0.0


def test_telemetry_report_totals(dataset_dir: Path) -> None:
    wh, _ = run(dataset_dir)
    reconcile(dataset_dir / "bank_statement.csv", dataset_dir / "gl_cash.csv", Settings(), wh)
    rep = telemetry_report(wh)
    assert rep["totals"]["runs"] == 2
    assert rep["totals"]["calls"] == sum(r["calls"] for r in rep["runs"])
    assert rep["totals"]["failure_rate"] == 0.0
    assert Decimal(rep["totals"]["analyst_minutes_saved_ASSUMPTION"]) > 0
    assert rep["runs"][0]["avg_latency_ms"] >= 0
    assert "not measurements" in rep["assumption_note"]
