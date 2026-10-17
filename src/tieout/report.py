"""Report rendering: report.html (sign-off ready), report.md, and result.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from tieout.models import BreakType
from tieout.money import fmt_money, to_money

TEMPLATES = Path(__file__).parent / "templates"

BREAK_LABELS = {
    BreakType.DEPOSIT_IN_TRANSIT.value: "Deposits in transit",
    BreakType.OUTSTANDING_CHECK.value: "Outstanding checks",
    BreakType.BANK_ERROR.value: "Bank errors",
    BreakType.INTEREST_UNRECORDED.value: "Interest not yet recorded",
    BreakType.BANK_FEE_UNRECORDED.value: "Bank fees not yet recorded",
    BreakType.NSF_RETURN_UNRECORDED.value: "NSF returns not yet recorded",
    BreakType.GL_DUPLICATE.value: "GL duplicate postings",
    BreakType.GL_TRANSPOSITION.value: "GL transposition errors",
    BreakType.GL_WRONG_AMOUNT.value: "GL wrong amounts",
    BreakType.UNIDENTIFIED.value: "Unidentified",
}
BREAK_ORDER = list(BREAK_LABELS)


def _money(value: Any, parens: bool = True) -> str:
    if value is None or value == "":
        return ""
    return fmt_money(to_money(str(value)), parens=parens)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = _money
    env.filters["abs_money"] = lambda v: _money(abs(to_money(str(v))), parens=False)
    env.filters["pct"] = lambda v: f"{v * 100:.1f}%"
    return env


def view_model(result: dict[str, Any]) -> dict[str, Any]:
    """Shape the result document for templates. No arithmetic beyond summing for display."""
    tie = result["tieout"]
    exceptions = {e["item_id"]: e for e in result["exceptions"]}
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in result["findings"]:
        fin = f["finding"]
        ids = [f["item_id"]] + ([fin["counterpart_id"]] if fin["counterpart_id"] else [])
        items = [exceptions[i] for i in ids if i in exceptions]
        groups.setdefault(fin["break_type"], []).append(
            {
                "exception_id": f["exception_id"],
                "items": items,
                "explanation": fin["explanation"],
                "evidence_ids": fin["evidence_ids"],
                "proposed_action": fin["proposed_action"],
                "confidence": fin["confidence"],
                "source": f["source"],
                "review_status": f["review_status"],
                "review_reasons": f["review_reasons"],
            }
        )
    schedule_amounts = {
        ln["exception_id"]: ln["amount"]
        for key in (
            "deposits_in_transit",
            "outstanding_checks",
            "bank_errors",
            "unrecorded_credits",
            "unrecorded_debits",
            "book_errors",
        )
        for ln in tie[key]
    }
    for rows in groups.values():
        for row in rows:
            row["schedule_amount"] = schedule_amounts.get(row["exception_id"])
    ordered = [
        {"break_type": bt, "label": BREAK_LABELS[bt], "rows": groups[bt]}
        for bt in BREAK_ORDER
        if bt in groups
    ]

    def total(key: str) -> Decimal:
        return to_money(sum((to_money(ln["amount"]) for ln in tie[key]), Decimal("0")))

    review_queue = [f for f in result["findings"] if f["review_status"] == "NEEDS_REVIEW"]
    meta = result["metadata"]
    return {
        "r": result,
        "tie": tie,
        "meta": meta,
        "groups": ordered,
        "totals": {
            k: total(k)
            for k in (
                "deposits_in_transit",
                "outstanding_checks",
                "bank_errors",
                "unrecorded_credits",
                "unrecorded_debits",
                "book_errors",
            )
        },
        "review_queue": review_queue,
        "pending_jes": [j for j in result["journal_entries"] if j["status"] == "PENDING_APPROVAL"],
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "tied": result["status"] == "TIED",
        "labels": BREAK_LABELS,
    }


def render_html(result: dict[str, Any]) -> str:
    return _env().get_template("report.html.j2").render(**view_model(result))


def render_markdown(result: dict[str, Any]) -> str:
    return _env().get_template("report.md.j2").render(**view_model(result))


def write_reports(result: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "html": out_dir / "report.html",
        "md": out_dir / "report.md",
        "json": out_dir / "result.json",
    }
    paths["html"].write_text(render_html(result), encoding="utf-8")
    paths["md"].write_text(render_markdown(result), encoding="utf-8")
    paths["json"].write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return paths
