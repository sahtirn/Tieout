from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.test_audit_review_telemetry import UnsureMock
from tieout.cli import app
from tieout.config import Settings
from tieout.pipeline import reconcile
from tieout.report import render_html, render_markdown
from tieout.warehouse import DuckDBWarehouse

runner = CliRunner()


@pytest.fixture()
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    res = runner.invoke(app, ["generate", "--seed", "42", "--out", "data"])
    assert res.exit_code == 0, res.output
    return tmp_path


def _reconcile(workdir: Path, *extra: str) -> tuple[str, Any]:
    res = runner.invoke(
        app,
        [
            "reconcile",
            "--bank",
            "data/bank_statement.csv",
            "--gl",
            "data/gl_cash.csv",
            "--preparer",
            "pat",
            *extra,
        ],
    )
    run_dirs = sorted((workdir / "runs").iterdir()) if (workdir / "runs").exists() else []
    return (run_dirs[-1].name if run_dirs else ""), res


def test_generate_reconcile_report_audit_telemetry(workdir: Path) -> None:
    assert (workdir / "data" / "labels.json").exists()
    run_id, res = _reconcile(workdir)
    assert res.exit_code == 0, res.output
    assert "TIED" in res.output
    run_dir = workdir / "runs" / run_id
    for name in ("report.html", "report.md", "result.json"):
        assert (run_dir / name).exists()
    result = json.loads((run_dir / "result.json").read_text())
    assert result["status"] == "TIED" and result["tieout"]["difference"] == "0.00"

    res = runner.invoke(app, ["report", run_id, "--out", "rerender"])
    assert res.exit_code == 0 and (workdir / "rerender" / "report.html").exists()
    res = runner.invoke(app, ["audit", run_id])
    assert res.exit_code == 0 and (run_dir / "audit.jsonl").exists()
    res = runner.invoke(app, ["telemetry"])
    assert res.exit_code == 0 and "TOTAL" in res.output
    res = runner.invoke(app, ["telemetry", "--json"])
    assert json.loads(res.output)["totals"]["runs"] == 1


def test_unknown_run_ids_fail_cleanly(workdir: Path) -> None:
    for cmd in (["report", "nope"], ["audit", "nope"], ["review", "nope", "--list"]):
        res = runner.invoke(app, cmd)
        assert res.exit_code == 1 and "not found" in res.output


def test_review_flow_non_interactive_and_interactive(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tieout.pipeline.make_provider", lambda s: UnsureMock())
    run_id, res = _reconcile(workdir, "--fail-on-unreconciled")
    assert res.exit_code == 2 and "UNRECONCILED" in res.output
    res = runner.invoke(app, ["review", run_id, "--list"])
    assert res.exit_code == 0 and "EXC-0001" in res.output
    res = runner.invoke(
        app, ["review", run_id, "--reviewer", "pat", "--item", "EXC-0001", "--decision", "approve"]
    )
    assert res.exit_code == 1 and "maker-checker" in res.output
    res = runner.invoke(app, ["review", run_id, "--reviewer", "riley", "--item", "EXC-0001"])
    assert res.exit_code == 1 and "--decision is required" in res.output
    res = runner.invoke(
        app,
        [
            "review",
            run_id,
            "--reviewer",
            "riley",
            "--item",
            "AJE-001",
            "--decision",
            "approve",
            "--note",
            "ok",
        ],
    )
    assert res.exit_code == 0 and "Recorded APPROVE on AJE-001" in res.output
    result = json.loads((workdir / "runs" / run_id / "result.json").read_text())
    pending = [f for f in result["findings"] if f["review_status"] == "NEEDS_REVIEW"]
    jes = [j for j in result["journal_entries"] if j["status"] == "PENDING_APPROVAL"]
    answers = "".join("approve\nverified\n" for _ in pending) + "".join("skip\n" for _ in jes)
    res = runner.invoke(app, ["review", run_id, "--reviewer", "riley"], input=answers)
    assert res.exit_code == 0, res.output
    result = json.loads((workdir / "runs" / run_id / "result.json").read_text())
    assert result["status"] == "TIED"
    assert result["summary"]["findings_by_status"]["APPROVED"] == len(pending)


def test_review_with_nothing_pending(workdir: Path) -> None:
    run_id, _ = _reconcile(workdir)
    result = json.loads((workdir / "runs" / run_id / "result.json").read_text())
    answers = "".join("approve\nposted\n" for _ in result["journal_entries"])
    res = runner.invoke(app, ["review", run_id, "--reviewer", "riley"], input=answers)
    assert res.exit_code == 0
    result = json.loads((workdir / "runs" / run_id / "result.json").read_text())
    assert {j["status"] for j in result["journal_entries"]} == {"APPROVED"}
    res = runner.invoke(app, ["review", run_id, "--reviewer", "riley"])
    assert "Nothing is pending review" in res.output


def test_html_report_is_self_contained_and_signable(dataset_dir: Path) -> None:
    result = reconcile(
        dataset_dir / "bank_statement.csv",
        dataset_dir / "gl_cash.csv",
        Settings(),
        DuckDBWarehouse(),
    )
    html = render_html(result)
    assert 'class="badge tied">TIED' in html
    assert "Prepared by: tieout-agent" in html and "Reviewed by" in html
    assert result["metadata"]["inputs"][0]["sha256"] in html
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "<link" not in html and "<script" not in html
    md = render_markdown(result)
    assert "**TIED**" in md and "Adjusted bank balance" in md


def test_unreconciled_report_shows_difference_prominently(dataset_dir: Path) -> None:
    result = reconcile(
        dataset_dir / "bank_statement.csv",
        dataset_dir / "gl_cash.csv",
        Settings(),
        DuckDBWarehouse(),
        provider=UnsureMock(),
    )
    html = render_html(result)
    assert "UNEXPLAINED DIFFERENCE" in html and 'class="badge unrec"' in html
    assert "Needs review queue" in html and "tieout review" in html
    assert "UNEXPLAINED DIFFERENCE" in render_markdown(result)
