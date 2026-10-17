from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tieout.api import create_app
from tieout.config import Settings


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    settings = Settings(db_path=str(tmp_path / "api.duckdb"), runs_dir=str(tmp_path / "runs"))
    return TestClient(create_app(settings))


def _files(dataset_dir: Path) -> dict[str, tuple[str, bytes, str]]:
    return {
        "bank_statement": (
            "bank.csv",
            (dataset_dir / "bank_statement.csv").read_bytes(),
            "text/csv",
        ),
        "gl_cash": ("gl.csv", (dataset_dir / "gl_cash.csv").read_bytes(), "text/csv"),
    }


def test_health_and_openapi(client: TestClient) -> None:
    res = client.get("/health")
    assert res.status_code == 200 and res.json()["status"] == "ok"
    spec = client.get("/openapi.json").json()
    assert {"/reconcile", "/runs/{run_id}", "/runs/{run_id}/report", "/health"} <= set(
        spec["paths"]
    )


def test_reconcile_then_fetch(client: TestClient, dataset_dir: Path, tmp_path: Path) -> None:
    res = client.post("/reconcile", files=_files(dataset_dir), data={"preparer": "pat"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "TIED" and body["difference"] == "0.00"
    assert body["summary"]["exceptions"] > 0
    run = client.get(body["result_url"])
    assert run.status_code == 200 and run.json()["metadata"]["preparer"] == "pat"
    report = client.get(body["report_url"])
    assert report.status_code == 200 and "text/html" in report.headers["content-type"]
    assert "TIED" in report.text
    assert (tmp_path / "runs" / body["run_id"] / "report.html").exists()


def test_errors(client: TestClient, dataset_dir: Path) -> None:
    assert client.get("/runs/missing").status_code == 404
    assert client.get("/runs/missing/report").status_code == 404
    bad = _files(dataset_dir)
    bad["gl_cash"] = ("gl.csv", b"entry_id,date\nGL-1,2026-09-01\n", "text/csv")
    res = client.post("/reconcile", files=bad)
    assert res.status_code == 422 and "missing columns" in res.json()["detail"]
    assert client.post("/reconcile", files={}).status_code == 422


def test_upload_size_limit(
    client: TestClient, dataset_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tieout.api.MAX_UPLOAD_BYTES", 10)
    res = client.post("/reconcile", files=_files(dataset_dir))
    assert res.status_code == 413
