from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tieout.agent.providers import MockProvider
from tieout.agent.providers.base import Message, ProviderResponse
from tieout.cli import app
from tieout.config import EvalThresholds, Settings, load_thresholds
from tieout.evals import check_thresholds, evaluate, render_markdown, run_eval


class ChaosProvider(MockProvider):
    """Simulates an unreliable model: hallucinated ids and side-impossible classifications."""

    def __init__(self, seed: int = 0, rate: float = 0.5) -> None:
        super().__init__(model="chaos")
        self.rng = random.Random(seed)
        self.rate = rate

    def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        resp = super().complete(system, messages, tools)
        call = resp.tool_call
        if call and call.name == "submit_finding" and self.rng.random() < self.rate:
            if self.rng.random() < 0.5:
                call.args["evidence_ids"] = [*call.args["evidence_ids"], "BNK-99999"]
            else:
                call.args["break_type"] = (
                    "DEPOSIT_IN_TRANSIT"
                    if call.args["break_type"] != "DEPOSIT_IN_TRANSIT"
                    else "BANK_FEE_UNRECORDED"
                )
        return resp


def test_mock_eval_meets_thresholds(tmp_path: Path) -> None:
    report, json_path, md_path = evaluate(3, Settings(), tmp_path, seed_start=40)
    assert report["failures"] == []
    m = report["metrics"]
    assert m["tie_out_rate"] == 1.0 and m["match"]["f1"] == 1.0
    assert m["classification_accuracy"] == 1.0 and m["adjusted_balance_accuracy"] == 1.0
    assert m["per_type_accuracy"]["GL_TRANSPOSITION"]["accuracy"] == 1.0
    assert json.loads(json_path.read_text())["runs"] == 3
    assert "PASS" in md_path.read_text()


def test_unreliable_model_is_contained_by_controls() -> None:
    """Corrupted answers never reach the schedule: they are retried, then fall back to review."""
    report = run_eval(
        3, Settings(), seed_start=7, provider_factory=lambda s: ChaosProvider(seed=1, rate=1.0)
    )
    m = report["metrics"]
    # Every finding failed validation twice and fell back, so all need review...
    assert m["needs_review_rate"] == 1.0
    # ...the fallback classifier still labels them correctly...
    assert m["classification_accuracy"] == 1.0
    # ...and nothing unreviewed counts, so the rec is honestly UNRECONCILED.
    assert m["tie_out_rate"] == 0.0
    assert m["match"]["f1"] == 1.0


def test_thresholds_and_markdown() -> None:
    report = run_eval(1, Settings(), seed_start=3)
    strict = EvalThresholds(tie_out_rate=1.01, match_f1=1.01, classification_accuracy=1.01)
    failures = check_thresholds(report, strict)
    assert len(failures) == 3
    report.update(thresholds=strict.model_dump(), failures=failures, generated_at="now")
    md = render_markdown(report)
    assert "FAIL" in md and "Confusion matrix" in md and "Threshold failures" in md
    assert set(load_thresholds()) >= {"mock", "gemini"}


def test_eval_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["eval", "--seeds", "1", "--out-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "All eval thresholds passed" in res.output
    monkeypatch.setattr(
        "tieout.evals.thresholds_for",
        lambda s: EvalThresholds(tie_out_rate=1.0, match_f1=1.01, classification_accuracy=0.95),
    )
    res = runner.invoke(app, ["eval", "--seeds", "1", "--out-dir", str(tmp_path)])
    assert res.exit_code == 1 and "THRESHOLD MISSED" in res.output
