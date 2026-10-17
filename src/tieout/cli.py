"""TieOut command line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from tieout.config import Settings
from tieout.money import fmt_money, to_money
from tieout.warehouse import DuckDBWarehouse

app = typer.Typer(
    help="TieOut: auditable AI agent for month-end bank reconciliations.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
console = Console()

DbOpt = Annotated[str | None, typer.Option("--db", help="DuckDB path (default: DB_PATH).")]


def _settings(db: str | None = None, provider: str | None = None) -> Settings:
    overrides: dict[str, Any] = {}
    if db:
        overrides["db_path"] = db
    if provider:
        overrides["llm_provider"] = provider
    return Settings(**overrides)


def _money(v: Any) -> str:
    return fmt_money(to_money(str(v)), parens=True)


def _print_result(result: dict[str, Any], out_dir: Path | None = None) -> None:
    tie = result["tieout"]
    tied = result["status"] == "TIED"
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim")
    t.add_column(justify="right")
    t.add_row("Balance per bank", _money(tie["bank_ending_balance"]))
    t.add_row("Adjusted bank balance", _money(tie["adjusted_bank_balance"]))
    t.add_row("Balance per books", _money(tie["gl_ending_balance"]))
    t.add_row("Adjusted book balance", _money(tie["adjusted_book_balance"]))
    t.add_row("[bold]Difference[/bold]", f"[bold]{_money(tie['difference'])}[/bold]")
    s = result["summary"]
    t.add_row("Matches", f"{s['matches']} ({s['matched_items']} items)")
    t.add_row("Exceptions / findings", f"{s['exceptions']} / {s['findings']}")
    t.add_row("Needs review", str(s["findings_by_status"]["NEEDS_REVIEW"]))
    t.add_row("Proposed JEs", str(s["journal_entries"]))
    t.add_row(
        "LLM calls / est. cost",
        f"{result['telemetry']['calls']} / ${result['telemetry']['cost_usd']}",
    )
    title = f"[bold {'green' if tied else 'red'}]{result['status']}[/] {result['period']}"
    console.print(Panel(t, title=title, subtitle=result["run_id"], expand=False))
    if out_dir is not None:
        console.print(f"Reports written to [bold]{out_dir}[/bold]")


@app.command()
def generate(
    seed: Annotated[int, typer.Option(help="Random seed.")] = 42,
    month: Annotated[str, typer.Option(help="Period as YYYY-MM.")] = "2026-09",
    out: Annotated[Path, typer.Option(help="Output directory.")] = Path("data"),
    breaks: Annotated[
        str | None,
        typer.Option(help="Override break counts, e.g. 'OUTSTANDING_CHECK=5,BANK_ERROR=1'."),
    ] = None,
) -> None:
    """Generate a synthetic bank statement, GL cash detail, and ground-truth labels."""
    from tieout.generator import generate as gen
    from tieout.generator import parse_break_overrides, write_dataset

    ds = gen(seed, month, parse_break_overrides(breaks))
    paths = write_dataset(ds, out)
    console.print(
        f"Generated {len(ds.bank)} bank lines and {len(ds.gl)} GL lines for {month} (seed {seed})."
    )
    for p in paths.values():
        console.print(f"  {p}")


@app.command()
def reconcile(
    bank: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Bank statement CSV.")],
    gl: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="GL cash detail CSV.")],
    db: DbOpt = None,
    provider: Annotated[
        str | None, typer.Option(help="mock or gemini (default: LLM_PROVIDER).")
    ] = None,
    preparer: Annotated[
        str, typer.Option(help="Name recorded as preparer (maker).")
    ] = "tieout-agent",
    period: Annotated[
        str | None, typer.Option(help="YYYY-MM; inferred from the bank file.")
    ] = None,
    runs_dir: Annotated[Path | None, typer.Option(help="Where run folders go.")] = None,
    fail_on_unreconciled: Annotated[
        bool, typer.Option(help="Exit with code 2 when the reconciliation does not tie.")
    ] = False,
) -> None:
    """Match, investigate exceptions, tie out, and write runs/<run_id>/ reports."""
    from tieout.pipeline import reconcile as run_reconcile
    from tieout.report import write_reports

    settings = _settings(db, provider)
    wh = DuckDBWarehouse(settings.db_path)
    try:
        result = run_reconcile(bank, gl, settings, wh, preparer=preparer, period=period)
    finally:
        wh.close()
    out_dir = (runs_dir or Path(settings.runs_dir)) / result["run_id"]
    write_reports(result, out_dir)
    _print_result(result, out_dir)
    if fail_on_unreconciled and result["status"] != "TIED":
        raise typer.Exit(code=2)


@app.command()
def report(
    run_id: str,
    db: DbOpt = None,
    out: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
) -> None:
    """Re-render reports for a run from stored state, including review decisions."""
    from tieout.report import write_reports

    settings = _settings(db)
    wh = DuckDBWarehouse(settings.db_path)
    try:
        if wh.get_run(run_id) is None:
            console.print(f"[red]Run {run_id} not found in {settings.db_path}[/red]")
            raise typer.Exit(code=1)
        result = _refresh(wh, run_id, settings)
    finally:
        wh.close()
    out_dir = out or Path(settings.runs_dir) / run_id
    write_reports(result, out_dir)
    _print_result(result, out_dir)


def _refresh(wh: DuckDBWarehouse, run_id: str, settings: Settings) -> dict[str, Any]:
    from tieout.pipeline import build_result

    run = wh.get_run(run_id)
    assert run is not None
    result = build_result(wh, run_id, settings)
    wh.update_run_result(run_id, result["status"], result)
    return result


@app.command()
def audit(
    run_id: str,
    db: DbOpt = None,
    out: Annotated[Path | None, typer.Option(help="JSONL output path.")] = None,
) -> None:
    """Export the JSONL audit trail for a run."""
    from tieout.audit import export_audit

    settings = _settings(db)
    wh = DuckDBWarehouse(settings.db_path)
    try:
        if wh.get_run(run_id) is None:
            console.print(f"[red]Run {run_id} not found in {settings.db_path}[/red]")
            raise typer.Exit(code=1)
        path = out or Path(settings.runs_dir) / run_id / "audit.jsonl"
        n = export_audit(wh, run_id, settings, path)
    finally:
        wh.close()
    console.print(f"Wrote {n} audit records to [bold]{path}[/bold]")


@app.command()
def review(
    run_id: str,
    reviewer: Annotated[str | None, typer.Option(help="Reviewer name (checker).")] = None,
    item: Annotated[
        str | None,
        typer.Option(help="Exception id (EXC-...) or JE id (AJE-...) to decide non-interactively."),
    ] = None,
    decision: Annotated[str | None, typer.Option(help="approve or reject.")] = None,
    note: Annotated[str, typer.Option(help="Reviewer note (required to reject).")] = "",
    list_only: Annotated[bool, typer.Option("--list", help="List pending items only.")] = False,
    db: DbOpt = None,
) -> None:
    """Approve or reject NEEDS_REVIEW findings and proposed JEs (maker-checker)."""
    from tieout.report import write_reports
    from tieout.review import ReviewError, record_review

    settings = _settings(db)
    wh = DuckDBWarehouse(settings.db_path)
    try:
        if wh.get_run(run_id) is None:
            console.print(f"[red]Run {run_id} not found in {settings.db_path}[/red]")
            raise typer.Exit(code=1)
        result = _refresh(wh, run_id, settings)
        queue = _pending(result)
        if list_only:
            _show_queue(queue)
            return
        name = reviewer or Prompt.ask("Reviewer name")
        try:
            if item:
                if not decision:
                    raise ReviewError("--decision is required with --item")
                target = "JE" if item.upper().startswith("AJE-") else "EXCEPTION"
                record_review(wh, run_id, target, item, name, decision, note)
                console.print(f"Recorded {decision.upper()} on {item} by {name}.")
            else:
                if not queue:
                    console.print("Nothing is pending review.")
                for target, target_id, summary in queue:
                    console.print(Panel(summary, title=f"{target} {target_id}", expand=False))
                    choice = Prompt.ask(
                        "Decision", choices=["approve", "reject", "skip"], default="skip"
                    )
                    if choice == "skip":
                        continue
                    why = Prompt.ask("Note", default="")
                    record_review(wh, run_id, target, target_id, name, choice, why)
        except ReviewError as err:
            console.print(f"[red]Review refused:[/red] {err}")
            raise typer.Exit(code=1) from err
        result = _refresh(wh, run_id, settings)
    finally:
        wh.close()
    out_dir = Path(settings.runs_dir) / run_id
    write_reports(result, out_dir)
    _print_result(result, out_dir)


def _pending(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    queue = []
    for f in result["findings"]:
        if f["review_status"] == "NEEDS_REVIEW":
            fin = f["finding"]
            queue.append(
                (
                    "EXCEPTION",
                    f["exception_id"],
                    f"{f['item_id']}: proposed {fin['break_type']} "
                    f"(confidence {fin['confidence']:.2f}, {f['source'].lower()})\n"
                    f"{fin['explanation']}\nReasons: {'; '.join(f['review_reasons'])}",
                )
            )
    for je in result["journal_entries"]:
        if je["status"] == "PENDING_APPROVAL":
            lines = "\n".join(
                f"  {ln['account']} {ln['account_name']:<28} Dr {ln['debit']:>12}  "
                f"Cr {ln['credit']:>12}"
                for ln in je["lines"]
            )
            queue.append(("JE", je["je_id"], f"{je['memo']}\n{lines}"))
    return queue


def _show_queue(queue: list[tuple[str, str, str]]) -> None:
    t = Table(title="Pending review")
    t.add_column("Type")
    t.add_column("Id")
    t.add_column("Summary")
    for target, target_id, summary in queue:
        t.add_row(target, target_id, summary.splitlines()[0])
    console.print(t)


@app.command()
def telemetry(
    db: DbOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """LLM usage, cost, latency, failure rate, and assumed analyst time saved."""
    from tieout.telemetry import telemetry_report

    settings = _settings(db)
    wh = DuckDBWarehouse(settings.db_path)
    try:
        rep = telemetry_report(wh)
    finally:
        wh.close()
    if as_json:
        console.print_json(json.dumps(rep))
        return
    tot = rep["totals"]
    t = Table(title="Telemetry by run")
    for col in (
        "Run",
        "Period",
        "Status",
        "Model",
        "Calls",
        "Tokens in/out",
        "Cost USD",
        "Avg ms",
        "Fail %",
        "Min saved*",
    ):
        t.add_column(col, justify="right" if col not in ("Run", "Status", "Model") else "left")
    for r in rep["runs"]:
        t.add_row(
            r["run_id"][:8],
            r["period"],
            r["status"] or "",
            r["model"],
            str(r["calls"]),
            f"{r['input_tokens']:,}/{r['output_tokens']:,}",
            r["cost_usd"],
            str(r["avg_latency_ms"]),
            f"{r['failure_rate'] * 100:.1f}",
            r["analyst_minutes_saved_ASSUMPTION"],
        )
    t.add_section()
    t.add_row(
        "TOTAL",
        "",
        f"{tot['runs']} runs",
        "",
        str(tot["calls"]),
        f"{tot['input_tokens']:,}/{tot['output_tokens']:,}",
        tot["cost_usd"],
        str(tot["avg_latency_ms"]),
        f"{tot['failure_rate'] * 100:.1f}",
        tot["analyst_minutes_saved_ASSUMPTION"],
    )
    console.print(t)
    console.print(f"[dim]* {rep['assumption_note']}[/dim]")


@app.command("eval")
def eval_cmd(
    seeds: Annotated[int, typer.Option(help="Number of seeds to evaluate.")] = 25,
    seed_start: Annotated[int, typer.Option(help="First seed.")] = 1,
    month: Annotated[str, typer.Option(help="Period as YYYY-MM.")] = "2026-09",
    provider: Annotated[str | None, typer.Option(help="mock or gemini.")] = None,
    out_dir: Annotated[Path, typer.Option(help="Results directory.")] = Path("evals/results"),
) -> None:
    """Score the full pipeline against ground truth across seeds; non-zero exit on a miss."""
    from tieout.evals import evaluate

    settings = _settings(provider=provider)

    def progress(seed: int, s: dict[str, Any]) -> None:
        console.print(
            f"seed {seed:>3}: {'TIED' if s['tied'] else 'UNRECONCILED':<12} "
            f"match F1 {s['match']['f1']:.4f}  classified "
            f"{s['classification_correct']}/{s['classification_total']}  "
            f"needs review {s['needs_review']}"
        )

    report, json_path, md_path = evaluate(seeds, settings, out_dir, seed_start, month, progress)
    m = report["metrics"]
    t = Table(title=f"Eval: {report['runs']} runs ({report['provider']})")
    t.add_column("Metric")
    t.add_column("Result", justify="right")
    t.add_column("Threshold", justify="right")
    th = report["thresholds"]
    t.add_row("Tie-out rate", f"{m['tie_out_rate']:.2%}", f"{th['tie_out_rate']:.2%}")
    t.add_row("Match F1", f"{m['match']['f1']:.4f}", f"{th['match_f1']:.2f}")
    t.add_row(
        "Classification accuracy",
        f"{m['classification_accuracy']:.4f}",
        f"{th['classification_accuracy']:.2f}",
    )
    t.add_row("Adjusted balance accuracy", f"{m['adjusted_balance_accuracy']:.2%}", "")
    t.add_row("NEEDS_REVIEW rate", f"{m['needs_review_rate']:.2%}", "")
    t.add_row("Est. cost per run (USD)", m["cost_usd_per_run"], "")
    console.print(t)
    console.print(f"Results: {json_path}\nSummary: {md_path}")
    if report["failures"]:
        for f in report["failures"]:
            console.print(f"[red]THRESHOLD MISSED[/red] {f}")
        raise typer.Exit(code=1)
    console.print("[green]All eval thresholds passed.[/green]")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:  # pragma: no cover - blocking server; exercised by the API tests and Phase 10 check
    """Run the HTTP API (OpenAPI docs at /docs)."""
    import uvicorn

    uvicorn.run("tieout.api:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
