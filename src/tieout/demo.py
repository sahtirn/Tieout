"""Public live demo: run a full reconciliation in the browser.

Stateless by design so it works on serverless hosts (each request may land on a fresh instance):
every request generates a seeded month, runs the whole pipeline against an in-memory warehouse,
and renders the result. The same seed always reproduces the same reconciliation, so the report,
audit, and data links simply re-derive it.

The demo always uses the deterministic MockProvider, whatever LLM_PROVIDER says, so a public URL
can never spend API credits. The mock runs the same tool-calling loop the Gemini agent uses.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from tieout import __version__
from tieout.agent.providers import MockProvider
from tieout.audit import audit_records
from tieout.config import Settings
from tieout.evals import score_run
from tieout.generator import generate, write_dataset
from tieout.io import InputError
from tieout.money import MoneyError, fmt_money, to_money
from tieout.pipeline import reconcile
from tieout.report import render_html
from tieout.warehouse import DuckDBWarehouse

DEMO_MONTH = "2026-09"
MAX_SEED = 999_999
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_ROWS = 5_000
DATA_FILES = {
    "bank_statement.csv": "text/csv",
    "gl_cash.csv": "text/csv",
    "labels.json": "application/json",
}
REPO_URL = "https://github.com/sahtirn/Tieout"
ITEM_LABELS = {
    "DEPOSIT_IN_TRANSIT": "Deposit in transit",
    "OUTSTANDING_CHECK": "Outstanding check",
    "BANK_ERROR": "Bank error",
    "INTEREST_UNRECORDED": "Interest not recorded",
    "BANK_FEE_UNRECORDED": "Bank fee not recorded",
    "NSF_RETURN_UNRECORDED": "NSF return not recorded",
    "GL_DUPLICATE": "GL duplicate",
    "GL_TRANSPOSITION": "GL transposition",
    "GL_WRONG_AMOUNT": "GL wrong amount",
    "UNIDENTIFIED": "Unidentified",
}
TEMPLATES = Path(__file__).parent / "templates"

SeedQuery = Annotated[int, Query(ge=1, le=MAX_SEED, description="Dataset seed")]


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = lambda v: fmt_money(to_money(str(v)), parens=True)
    return env


def _demo_settings(base: Settings) -> Settings:
    return base.model_copy(update={"llm_provider": "mock"})


def _run(bank: Path, gl: Path, settings: Settings) -> dict[str, Any]:
    """Run the pipeline in memory; return the result, agent trace, and audit lines."""
    wh = DuckDBWarehouse(":memory:")
    try:
        started = time.perf_counter()
        result = reconcile(bank, gl, settings, wh, provider=MockProvider(), preparer="tieout-demo")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        steps = wh.get_agent_steps(result["run_id"])
        audit = [
            json.dumps(r, sort_keys=True, default=str)
            for r in audit_records(wh, result["run_id"], settings)
        ]
    finally:
        wh.close()
    trace: dict[str, list[dict[str, Any]]] = {}
    for s in steps:
        trace.setdefault(s.exception_id, []).append(
            {
                "step_no": s.step_no,
                "step_type": s.step_type,
                "tool": s.tool_name,
                "args": summarize_args(s.arguments),
                "result": summarize_result(s.tool_name, s.step_type, s.result, s.arguments),
            }
        )
    return {"result": result, "trace": trace, "audit": audit, "elapsed_ms": elapsed_ms}


def run_seed(seed: int, settings: Settings) -> dict[str, Any]:
    """Generate, reconcile, and grade one seeded month (deterministic apart from ids and times)."""
    with tempfile.TemporaryDirectory(prefix=f"tieout-demo-{seed}-") as tmp:
        paths = write_dataset(generate(seed, DEMO_MONTH), Path(tmp))
        out = _run(paths["bank"], paths["gl"], settings)
        labels = json.loads(paths["labels"].read_text(encoding="utf-8"))
        files = {name: (Path(tmp) / name).read_bytes() for name in DATA_FILES}
    out["score"] = score_run(out["result"], labels)
    out["files"] = files
    out["seed"] = seed
    return out


def summarize_args(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    keep = {k: v for k, v in args.items() if v not in (None, "")}
    parts = []
    for k, v in keep.items():
        text = v if isinstance(v, str) else json.dumps(v)
        if len(text) > 48:
            text = text[:45] + "..."
        parts.append(f"{k}={text}")
    return ", ".join(parts)


def summarize_result(
    tool: str | None, step_type: str, result: Any, args: dict[str, Any] | None
) -> str:
    """One readable line per step for the agent trace."""
    if not isinstance(result, dict):
        return ""
    if step_type == "invalid_finding":
        return f"rejected by controls: {result.get('error', '')}"
    if "error" in result:
        return f"error: {result['error']}"
    if step_type in ("final", "fallback_final"):
        bt = (args or {}).get("break_type", "")
        conf = (args or {}).get("confidence")
        note = f" (confidence {conf:.2f})" if isinstance(conf, int | float) else ""
        status = result.get("review_status") or (
            "NEEDS_REVIEW" if step_type == "fallback_final" else ""
        )
        return f"{ITEM_LABELS.get(bt, bt)}{note}; passed validation; {status}".strip("; ")
    if tool == "get_transaction":
        return (
            f"{result.get('description', '')}, {result.get('amount', '')}, "
            f"{str(result.get('status', '')).lower()}"
        )
    if tool == "describe_bank_codes":
        codes = result.get("codes", [])
        return (
            "; ".join(
                c["code"] + (f" suggests {c['suggests']}" if c.get("suggests") else "")
                for c in codes
            )
            or "no recognized bank codes"
        )
    if tool == "check_transposition":
        cands = result.get("candidates", [])
        if not cands:
            return "no digit-swap candidates in the other ledger"
        c = cands[0]
        return (
            f"{len(cands)} candidate(s): {c['id']} at {c['amount']} "
            f"(difference {c.get('difference')}, divisible by 9)"
        )
    if tool == "find_duplicates":
        dups = result.get("duplicates", [])
        if not dups:
            return "no duplicate postings"
        d = dups[0]
        return f"twin {d['id']} ({str(d['status']).lower()}, same reference: {d['same_reference']})"
    if tool == "search_ledger":
        rows = result.get("results", [])
        if not rows:
            return "no matching rows"
        r = rows[0]
        return (
            f"{result.get('count', len(rows))} hit(s); top: {r['id']} {r['description']} "
            f"{r['amount']} ({str(r['status']).lower()})"
        )
    return ""


def build_view(run: dict[str, Any], *, seed: int | None, filename_note: str = "") -> dict[str, Any]:
    result = run["result"]
    tie = result["tieout"]
    summary = result["summary"]
    rules = summary["matches_by_rule"]
    findings = {f["exception_id"]: f for f in result["findings"]}
    items = []
    for exc in result["exceptions"]:
        f = findings.get(exc["exception_id"])
        if f is None:
            continue  # counterpart of a paired finding; shown with that finding
        fin = f["finding"]
        items.append(
            {
                "exception_id": exc["exception_id"],
                "item_id": exc["item_id"],
                "side": exc["side"],
                "description": exc["description"],
                "amount": exc["amount"],
                "break_type": fin["break_type"],
                "label": ITEM_LABELS.get(fin["break_type"], fin["break_type"]),
                "counterpart": fin["counterpart_id"],
                "explanation": fin["explanation"],
                "evidence": fin["evidence_ids"],
                "review_status": f["review_status"],
                "steps": run["trace"].get(exc["exception_id"], []),
            }
        )
    agent_steps = sum(len(v) for v in run["trace"].values())
    stages = [
        {
            "title": "Load",
            "metric": f"{summary['bank_lines']} + {summary['gl_lines']}",
            "detail": "bank and GL lines; running balances verified",
        },
        {
            "title": "Match",
            "metric": str(summary["matches"]),
            "detail": f"{rules.get('EXACT', 0)} exact, {rules.get('NEAR', 0)} near, "
            f"{rules.get('MANY_TO_ONE', 0)} batch deposits",
        },
        {
            "title": "Investigate",
            "metric": str(summary["exceptions"]),
            "detail": f"exceptions; {agent_steps} agent steps using read-only tools",
        },
        {
            "title": "Verify",
            "metric": str(summary["findings"]),
            "detail": f"findings passed code controls; "
            f"{summary['findings_by_status']['NEEDS_REVIEW']} need review",
        },
        {
            "title": "Tie out",
            "metric": fmt_money(to_money(tie["difference"]), parens=True),
            "detail": "difference, computed in Decimal by code",
        },
        {
            "title": "Propose",
            "metric": str(summary["journal_entries"]),
            "detail": "balanced journal entries awaiting human approval",
        },
    ]
    score = run.get("score")
    totals = {
        key: to_money(sum((to_money(ln["amount"]) for ln in tie[key]), Decimal("0")))
        for key in (
            "deposits_in_transit",
            "outstanding_checks",
            "bank_errors",
            "unrecorded_credits",
            "unrecorded_debits",
            "book_errors",
        )
    }
    return {
        "r": result,
        "tie": tie,
        "seed": seed,
        "elapsed_ms": run["elapsed_ms"],
        "stages": stages,
        "items": items,
        "score": score,
        "totals": totals,
        "filename_note": filename_note,
        "report_srcdoc": render_html(result) if seed is None else "",
    }


def _page(name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("upload_error", "")
    html = (
        _env()
        .get_template(name)
        .render(repo_url=REPO_URL, version=__version__, max_seed=MAX_SEED, **ctx)
    )
    return HTMLResponse(html, status_code=status_code)


def make_router(settings: Settings) -> APIRouter:
    demo_settings = _demo_settings(settings)
    router = APIRouter(tags=["demo"])

    @lru_cache(maxsize=32)
    def cached(seed: int) -> dict[str, Any]:
        return run_seed(seed, demo_settings)

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home() -> HTMLResponse:
        return _page("demo_home.html.j2", suggested_seed=secrets.randbelow(MAX_SEED) + 1)

    @router.get("/demo/run", response_model=None)
    async def demo_run(
        seed: Annotated[int | None, Query(ge=1, le=MAX_SEED)] = None,
    ) -> HTMLResponse | RedirectResponse:
        """Generate a seeded month, reconcile it end to end, and show the agent's work."""
        if seed is None:
            return RedirectResponse(
                f"/demo/run?seed={secrets.randbelow(MAX_SEED) + 1}", status_code=307
            )
        run = await run_in_threadpool(cached, seed)
        return _page("demo_result.html.j2", **build_view(run, seed=seed))

    @router.get("/demo/report", response_class=HTMLResponse)
    async def demo_report(seed: SeedQuery) -> HTMLResponse:
        """The full sign-off workpaper for a seeded demo month."""
        run = await run_in_threadpool(cached, seed)
        return HTMLResponse(render_html(run["result"]))

    @router.get("/demo/audit", response_class=PlainTextResponse)
    async def demo_audit(seed: SeedQuery) -> Response:
        """JSONL audit trail for a seeded demo month."""
        run = await run_in_threadpool(cached, seed)
        return Response(
            "\n".join(run["audit"]) + "\n",
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="tieout-audit-seed{seed}.jsonl"'
            },
        )

    @router.get("/demo/data/{seed}/{name}")
    async def demo_data(seed: Annotated[int, PathParam(ge=1, le=MAX_SEED)], name: str) -> Response:
        """Download the generated inputs or the ground-truth labels for a seed."""
        if name not in DATA_FILES:
            raise HTTPException(404, f"unknown file {name}")
        run = await run_in_threadpool(cached, seed)
        return Response(
            run["files"][name],
            media_type=DATA_FILES[name],
            headers={"Content-Disposition": f'attachment; filename="seed{seed}-{name}"'},
        )

    @router.post("/demo/upload", response_class=HTMLResponse)
    async def demo_upload(
        bank_statement: UploadFile = File(..., description="Bank statement CSV"),
        gl_cash: UploadFile = File(..., description="GL cash detail CSV"),
    ) -> HTMLResponse:
        """Reconcile your own two CSVs. Nothing is stored; the result is returned inline."""
        with tempfile.TemporaryDirectory(prefix="tieout-demo-upload-") as tmp:
            paths: dict[str, Path] = {}
            for name, upload in (("bank", bank_statement), ("gl", gl_cash)):
                data = await upload.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"each file must be under {MAX_UPLOAD_BYTES} bytes")
                if data.count(b"\n") > MAX_UPLOAD_ROWS:
                    raise HTTPException(413, f"each file must have at most {MAX_UPLOAD_ROWS} rows")
                paths[name] = Path(tmp) / f"{name}.csv"
                paths[name].write_bytes(data)
            try:
                run = await run_in_threadpool(_run, paths["bank"], paths["gl"], demo_settings)
            except (InputError, MoneyError, ValueError, KeyError) as err:
                return _page(
                    "demo_home.html.j2",
                    status_code=422,
                    suggested_seed=42,
                    upload_error=f"Could not reconcile those files: {err}",
                )
        note = f"{bank_statement.filename} and {gl_cash.filename}"
        return _page("demo_result.html.j2", **build_view(run, seed=None, filename_note=note))

    return router
