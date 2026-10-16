"""JSONL audit trail export.

One JSON object per line. Every record has a `record` type. The `reconciling_item_trace` records
let a reviewer follow any reconciling item end to end: source rows -> exception -> agent steps ->
final classification -> schedule placement -> proposed JE -> review decisions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tieout.config import Settings
from tieout.pipeline import build_result
from tieout.warehouse.base import Warehouse

SCHEDULE_SECTIONS = {
    "deposits_in_transit": "BANK: add deposits in transit",
    "outstanding_checks": "BANK: less outstanding checks",
    "bank_errors": "BANK: bank errors",
    "unrecorded_credits": "BOOK: add unrecorded bank credits",
    "unrecorded_debits": "BOOK: less unrecorded bank debits",
    "book_errors": "BOOK: book errors",
}


def _row(item: Any) -> dict[str, Any]:
    data: dict[str, Any] = item.model_dump(mode="json")
    return data


def audit_records(wh: Warehouse, run_id: str, settings: Settings) -> Iterator[dict[str, Any]]:
    result = build_result(wh, run_id, settings)
    bank = {t.txn_id: t for t in wh.get_bank(run_id)}
    gl = {e.entry_id: e for e in wh.get_gl(run_id)}
    matches = wh.get_matches(run_id)
    steps = wh.get_agent_steps(run_id)
    calls = wh.get_llm_calls(run_id)

    yield {
        "record": "run",
        "run_id": run_id,
        "status": result["status"],
        "metadata": result["metadata"],
        "summary": result["summary"],
    }

    match_of: dict[str, str] = {}
    for m in matches:
        for i in [*m.bank_ids, *m.gl_ids]:
            match_of[i] = m.match_id
    exc_of = {e["item_id"]: e["exception_id"] for e in result["exceptions"]}
    for txn in bank.values():
        yield {
            "record": "source_row",
            "ledger": "BANK",
            **_row(txn),
            "match_id": match_of.get(txn.txn_id),
            "exception_id": exc_of.get(txn.txn_id),
        }
    for entry in gl.values():
        yield {
            "record": "source_row",
            "ledger": "GL",
            **_row(entry),
            "match_id": match_of.get(entry.entry_id),
            "exception_id": exc_of.get(entry.entry_id),
        }
    for m in result["matches"]:
        yield {"record": "match", **m}
    for e in result["exceptions"]:
        yield {"record": "exception", **e}
    for s in steps:
        yield {
            "record": "agent_step",
            "exception_id": s.exception_id,
            "step_no": s.step_no,
            "step_type": s.step_type,
            "tool_name": s.tool_name,
            "arguments": s.arguments,
            "result": s.result,
            "created_at": s.created_at.isoformat(),
        }
    for c in calls:
        yield {
            "record": "llm_call",
            "call_id": c.call_id,
            "exception_id": c.exception_id,
            "created_at": c.created_at.isoformat(),
            "provider": c.provider,
            "model": c.model,
            "prompt_version": c.prompt_version,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "latency_ms": c.latency_ms,
            "cost_usd": format(c.cost_usd, "f"),
            "success": c.success,
            "retry_count": c.retry_count,
            "error": c.error,
        }
    for f in result["findings"]:
        yield {"record": "finding", **f}
    for je in result["journal_entries"]:
        yield {"record": "journal_entry", **je}
    for a in result["review_actions"]:
        yield {"record": "review_action", **a}

    placement: dict[str, dict[str, Any]] = {}
    for key, label in SCHEDULE_SECTIONS.items():
        for line in result["tieout"][key]:
            placement[line["exception_id"]] = {"section": label, "amount": line["amount"]}
    findings = {f["exception_id"]: f for f in result["findings"]}
    jes: dict[str, list[str]] = {}
    for je in result["journal_entries"]:
        jes.setdefault(je["exception_id"], []).append(je["je_id"])
    for e in result["exceptions"]:
        f = findings.get(e["exception_id"])
        if f is None:
            continue  # explained as the counterpart of another finding; traced there
        item_ids = [f["item_id"]] + (
            [f["finding"]["counterpart_id"]] if f["finding"]["counterpart_id"] else []
        )
        rows = [
            _row(bank[i]) | {"ledger": "BANK"} if i in bank else _row(gl[i]) | {"ledger": "GL"}
            for i in item_ids
        ]
        je_ids = jes.get(e["exception_id"], [])
        yield {
            "record": "reconciling_item_trace",
            "exception_id": e["exception_id"],
            "item_ids": item_ids,
            "source_rows": rows,
            "agent_steps": [
                {"step_no": s.step_no, "step_type": s.step_type, "tool_name": s.tool_name}
                for s in steps
                if s.exception_id == e["exception_id"]
            ],
            "classification": f["finding"]["break_type"],
            "classification_source": f["source"],
            "confidence": f["finding"]["confidence"],
            "review_status": f["review_status"],
            "explanation": f["finding"]["explanation"],
            "evidence_ids": f["finding"]["evidence_ids"],
            "schedule": placement.get(
                e["exception_id"], {"section": "UNEXPLAINED (not counted)", "amount": None}
            ),
            "journal_entry_ids": je_ids,
            "review_actions": [
                a
                for a in result["review_actions"]
                if a["target_id"] in [e["exception_id"], *je_ids]
            ],
        }
    yield {"record": "tieout", **result["tieout"]}


def export_audit(wh: Warehouse, run_id: str, settings: Settings, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for rec in audit_records(wh, run_id, settings):
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            n += 1
    return n
