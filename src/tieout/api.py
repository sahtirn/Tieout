"""Headless HTTP API. OpenAPI docs are served at /docs and /openapi.json."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from tieout import __version__
from tieout.config import Settings
from tieout.io import InputError
from tieout.money import MoneyError
from tieout.pipeline import build_result, reconcile
from tieout.report import render_html, write_reports
from tieout.warehouse import DuckDBWarehouse

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class ReconcileResponse(BaseModel):
    run_id: str
    status: str
    period: str
    difference: str
    summary: dict[str, Any]
    report_url: str
    result_url: str


class Health(BaseModel):
    status: str
    version: str
    provider: str


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    # DuckDB allows one writer per file; serialize warehouse access within this process.
    lock = threading.Lock()
    app = FastAPI(
        title="TieOut API",
        version=__version__,
        description="Auditable month-end bank reconciliation. Deterministic matching and "
        "arithmetic; an LLM only investigates and explains exceptions.",
    )

    def open_wh() -> DuckDBWarehouse:
        return DuckDBWarehouse(cfg.db_path)

    @app.get("/health", response_model=Health)
    def health() -> Health:
        return Health(status="ok", version=__version__, provider=cfg.llm_provider)

    @app.post("/reconcile", response_model=ReconcileResponse)
    async def post_reconcile(
        bank_statement: UploadFile = File(..., description="Bank statement CSV"),
        gl_cash: UploadFile = File(..., description="GL cash detail CSV"),
        preparer: str = Form("tieout-api"),
    ) -> ReconcileResponse:
        with tempfile.TemporaryDirectory(prefix="tieout-upload-") as tmp:
            paths = {}
            for name, upload in (("bank_statement.csv", bank_statement), ("gl_cash.csv", gl_cash)):
                data = await upload.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"{name} exceeds {MAX_UPLOAD_BYTES} bytes")
                path = Path(tmp) / name
                path.write_bytes(data)
                paths[name] = path
            # The pipeline is blocking (DuckDB, CPU, provider calls): keep it off the event loop.
            result = await run_in_threadpool(run_pipeline, paths, preparer)
        write_reports(result, Path(cfg.runs_dir) / result["run_id"])
        rid = result["run_id"]
        return ReconcileResponse(
            run_id=rid,
            status=result["status"],
            period=result["period"],
            difference=result["tieout"]["difference"],
            summary=result["summary"],
            report_url=f"/runs/{rid}/report",
            result_url=f"/runs/{rid}",
        )

    def run_pipeline(paths: dict[str, Path], preparer: str) -> dict[str, Any]:
        with lock:
            wh = open_wh()
            try:
                return reconcile(
                    paths["bank_statement.csv"], paths["gl_cash.csv"], cfg, wh, preparer=preparer
                )
            except (InputError, MoneyError, ValueError, KeyError) as err:
                raise HTTPException(422, f"invalid input: {err}") from err
            finally:
                wh.close()

    def load(run_id: str) -> dict[str, Any]:
        with lock:
            wh = open_wh()
            try:
                if wh.get_run(run_id) is None:
                    raise HTTPException(404, f"run {run_id} not found")
                return build_result(wh, run_id, cfg)
            finally:
                wh.close()

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        return load(run_id)

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    def get_report(run_id: str) -> HTMLResponse:
        return HTMLResponse(render_html(load(run_id)))

    return app
