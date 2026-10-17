from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tieout.api import create_app
from tieout.config import Settings
from tieout.demo import summarize_args, summarize_result

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    # Gemini configured on purpose: the public demo must still never call it.
    settings = Settings(
        db_path=str(tmp_path / "d.duckdb"),
        runs_dir=str(tmp_path / "runs"),
        llm_provider="gemini",
        gemini_api_key="",
    )
    return TestClient(create_app(settings), follow_redirects=False)


def test_home_page(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "Run a reconciliation" in res.text and 'action="/demo/upload"' in res.text
    assert "mock mode" in res.text


def test_run_redirects_to_a_shareable_seed(client: TestClient) -> None:
    res = client.get("/demo/run")
    assert res.status_code == 307
    assert res.headers["location"].startswith("/demo/run?seed=")


def test_seeded_run_shows_stages_grade_and_agent_trace(client: TestClient) -> None:
    res = client.get("/demo/run?seed=42")
    assert res.status_code == 200
    html = res.text
    assert "Reconciled. Difference 0.00." in html and 'class="badge tied"' in html
    assert "Problems classified: 25 / 25" in html
    assert "0 wrong, 0 missed" in html and "Adjusted balance matches ground truth" in html
    assert html.count('class="case"') == 21  # one per finding; paired items shown together
    for tool in (
        "get_transaction",
        "describe_bank_codes",
        "check_transposition",
        "find_duplicates",
        "search_ledger",
        "submit_finding",
    ):
        assert tool in html
    assert "/demo/report?seed=42" in html and "/demo/data/42/labels.json" in html


def test_report_audit_and_data_downloads(client: TestClient) -> None:
    report = client.get("/demo/report?seed=42")
    assert report.status_code == 200 and "Prepared by: tieout-demo" in report.text
    audit = client.get("/demo/audit?seed=42")
    assert audit.status_code == 200
    records = [json.loads(line) for line in audit.text.splitlines()]
    assert records[0]["record"] == "run" and records[-1]["record"] == "tieout"
    labels = client.get("/demo/data/42/labels.json")
    assert labels.status_code == 200 and json.loads(labels.content)["seed"] == 42
    bank = client.get("/demo/data/42/bank_statement.csv")
    assert bank.text.startswith("txn_id,date,description")
    assert client.get("/demo/data/42/secrets.txt").status_code == 404
    assert client.get("/demo/data/0/labels.json").status_code == 422
    assert client.get("/demo/run?seed=0").status_code == 422
    assert client.get("/demo/report?seed=1000000").status_code == 422


def test_upload_runs_inline_and_embeds_report(client: TestClient, dataset_dir: Path) -> None:
    files = {
        "bank_statement": ("bank.csv", (dataset_dir / "bank_statement.csv").read_bytes()),
        "gl_cash": ("gl.csv", (dataset_dir / "gl_cash.csv").read_bytes()),
    }
    res = client.post("/demo/upload", files=files)
    assert res.status_code == 200
    assert "Your files: bank.csv and gl.csv" in res.text
    assert "not graded" in res.text and "<iframe" in res.text


def test_upload_errors(
    client: TestClient, dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = {
        "bank_statement": ("bank.csv", (dataset_dir / "bank_statement.csv").read_bytes()),
        "gl_cash": ("gl.csv", b"entry_id,date\nGL-1,2026-09-01\n"),
    }
    res = client.post("/demo/upload", files=bad)
    assert res.status_code == 422 and "Could not reconcile those files" in res.text
    monkeypatch.setattr("tieout.demo.MAX_UPLOAD_ROWS", 10)
    good = {
        "bank_statement": ("bank.csv", (dataset_dir / "bank_statement.csv").read_bytes()),
        "gl_cash": ("gl.csv", (dataset_dir / "gl_cash.csv").read_bytes()),
    }
    assert client.post("/demo/upload", files=good).status_code == 413
    monkeypatch.setattr("tieout.demo.MAX_UPLOAD_BYTES", 10)
    assert client.post("/demo/upload", files=good).status_code == 413


def test_step_summaries_cover_edge_cases() -> None:
    assert summarize_args(None) == ""
    assert summarize_args({"text": "x" * 80, "limit": 5, "empty": ""}).startswith("text=xxx")
    assert summarize_result("get_transaction", "tool_call", "not a dict", None) == ""
    assert summarize_result(None, "provider_error", {"error": "boom"}, None) == "error: boom"
    assert summarize_result(None, "invalid_finding", {"error": "bad id"}, None).startswith(
        "rejected by controls"
    )
    assert summarize_result("describe_bank_codes", "tool_call", {"codes": []}, None) == (
        "no recognized bank codes"
    )
    assert "no digit-swap" in summarize_result(
        "check_transposition", "tool_call", {"candidates": []}, None
    )
    assert summarize_result("find_duplicates", "tool_call", {"duplicates": []}, None) == (
        "no duplicate postings"
    )
    assert summarize_result("search_ledger", "tool_call", {"results": []}, None) == (
        "no matching rows"
    )
    assert summarize_result("other_tool", "tool_call", {"x": 1}, None) == ""
    final = summarize_result(
        None,
        "fallback_final",
        {"reason": "x"},
        {"break_type": "OUTSTANDING_CHECK", "confidence": 0.9},
    )
    assert final.startswith("Outstanding check (confidence 0.90)") and "NEEDS_REVIEW" in final


def test_vercel_entrypoint_exposes_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in (
        ("LLM_PROVIDER", "mock"),
        ("DB_PATH", str(tmp_path / "v.duckdb")),
        ("RUNS_DIR", str(tmp_path / "runs")),
    ):
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("vercel_app", ROOT / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert isinstance(module.app, FastAPI)
    assert TestClient(module.app).get("/health").json()["provider"] == "mock"
