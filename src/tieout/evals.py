"""Evaluation harness: generate datasets across seeds, run the full pipeline, score against labels.

Metrics
* Match precision / recall / F1 over (bank_id, gl_id) pairs (many-to-one expands to pairs).
* Classification accuracy over every labeled break item plus every false exception, with per-type
  accuracy and a confusion matrix. A paired finding classifies both of its items.
* Tie-out rate: share of runs with status TIED.
* Adjusted balance accuracy: share of runs whose adjusted bank and book balances both equal the
  ground-truth expected adjusted balance to the cent (a tie alone does not prove this; see D-013).
* NEEDS_REVIEW rate, LLM cost per run, and latency per run.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tieout.agent.providers import LLMProvider, make_provider
from tieout.config import EvalThresholds, Settings, load_thresholds
from tieout.generator import generate, write_dataset
from tieout.pipeline import reconcile
from tieout.warehouse import DuckDBWarehouse

MATCHED = "MATCHED"
UNCLASSIFIED = "UNCLASSIFIED"
NONE = "NONE"  # truth label for an item that should have matched but became an exception


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def score_run(result: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    truth_pairs = {(b, g) for m in labels["matches"] for b in m["bank_ids"] for g in m["gl_ids"]}
    pred_pairs = {(b, g) for m in result["matches"] for b in m["bank_ids"] for g in m["gl_ids"]}
    tp = len(truth_pairs & pred_pairs)

    truth_items = {
        i: br["break_type"] for br in labels["breaks"] for i in br["bank_ids"] + br["gl_ids"]
    }
    predicted: dict[str, str] = {}
    for f in result["findings"]:
        bt = f["finding"]["break_type"]
        predicted[f["item_id"]] = bt
        if f["finding"]["counterpart_id"]:
            predicted.setdefault(f["finding"]["counterpart_id"], bt)
    matched_items = {i for m in result["matches"] for i in [*m["bank_ids"], *m["gl_ids"]]}
    pairs: list[tuple[str, str]] = []
    for item, truth in truth_items.items():
        pred = predicted.get(item) or (MATCHED if item in matched_items else UNCLASSIFIED)
        pairs.append((truth, pred))
    for exc in result["exceptions"]:
        if exc["item_id"] not in truth_items:
            pairs.append((NONE, predicted.get(exc["item_id"], UNCLASSIFIED)))

    expected = Decimal(labels["expected_adjusted_balance"])
    tie = result["tieout"]
    adj_bank, adj_book = (
        Decimal(tie["adjusted_bank_balance"]),
        Decimal(tie["adjusted_book_balance"]),
    )
    findings = result["findings"]
    return {
        "match": _prf(tp, len(pred_pairs - truth_pairs), len(truth_pairs - pred_pairs)),
        "classification_pairs": pairs,
        "classification_correct": sum(1 for t, p in pairs if t == p),
        "classification_total": len(pairs),
        "tied": result["status"] == "TIED",
        "difference": tie["difference"],
        "adjusted_balance_correct": adj_bank == expected and adj_book == expected,
        "adjusted_balance_abs_error": format(abs(adj_bank - expected), "f"),
        "findings": len(findings),
        "needs_review": sum(1 for f in findings if f["review_status"] == "NEEDS_REVIEW"),
        "llm_calls": result["telemetry"]["calls"],
        "cost_usd": result["telemetry"]["cost_usd"],
        "avg_llm_latency_ms": result["telemetry"]["avg_latency_ms"],
    }


def run_eval(
    seeds: int,
    settings: Settings,
    seed_start: int = 1,
    month: str = "2026-09",
    provider_factory: Callable[[Settings], LLMProvider] = make_provider,
    progress: Callable[[int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    per_seed = []
    tp = fp = fn = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for seed in range(seed_start, seed_start + seeds):
        with tempfile.TemporaryDirectory(prefix=f"tieout-eval-{seed}-") as tmp:
            paths = write_dataset(generate(seed, month), Path(tmp))
            labels = json.loads(paths["labels"].read_text(encoding="utf-8"))
            wh = DuckDBWarehouse()
            started = time.perf_counter()
            result = reconcile(
                paths["bank"],
                paths["gl"],
                settings,
                wh,
                provider=provider_factory(settings),
                preparer="tieout-eval",
            )
            wall_ms = int((time.perf_counter() - started) * 1000)
            wh.close()
        s = score_run(result, labels)
        s["seed"] = seed
        s["run_wall_ms"] = wall_ms
        tp += s["match"]["tp"]
        fp += s["match"]["fp"]
        fn += s["match"]["fn"]
        for truth, pred in s.pop("classification_pairs"):
            confusion[truth][pred] += 1
        per_seed.append(s)
        if progress:
            progress(seed, s)

    n = len(per_seed)
    correct = sum(s["classification_correct"] for s in per_seed)
    total = sum(s["classification_total"] for s in per_seed)
    findings = sum(s["findings"] for s in per_seed)
    per_type = {
        truth: {
            "n": sum(row.values()),
            "correct": row.get(truth, 0),
            "accuracy": round(row.get(truth, 0) / sum(row.values()), 4),
        }
        for truth, row in sorted(confusion.items())
    }
    total_cost = sum((Decimal(s["cost_usd"]) for s in per_seed), Decimal("0"))
    return {
        "provider": settings.llm_provider,
        "model": provider_factory(settings).model,
        "prompt_version": settings.prompt_version,
        "seeds": [seed_start, seed_start + seeds - 1],
        "month": month,
        "runs": n,
        "metrics": {
            "match": _prf(tp, fp, fn),
            "classification_accuracy": round(correct / total, 4) if total else 1.0,
            "classification_items": total,
            "per_type_accuracy": per_type,
            "confusion_matrix": {
                t: dict(sorted(row.items())) for t, row in sorted(confusion.items())
            },
            "tie_out_rate": round(sum(1 for s in per_seed if s["tied"]) / n, 4) if n else 0.0,
            "adjusted_balance_accuracy": round(
                sum(1 for s in per_seed if s["adjusted_balance_correct"]) / n, 4
            )
            if n
            else 0.0,
            "needs_review_rate": round(sum(s["needs_review"] for s in per_seed) / findings, 4)
            if findings
            else 0.0,
            "cost_usd_total": format(total_cost, "f"),
            "cost_usd_per_run": format((total_cost / n).quantize(Decimal("0.000001")), "f")
            if n
            else "0",
            "llm_calls_per_run": round(sum(s["llm_calls"] for s in per_seed) / n, 2) if n else 0,
            "avg_run_wall_ms": sum(s["run_wall_ms"] for s in per_seed) // n if n else 0,
            "avg_llm_latency_ms": sum(s["avg_llm_latency_ms"] for s in per_seed) // n if n else 0,
        },
        "per_seed": per_seed,
    }


def check_thresholds(report: dict[str, Any], thresholds: EvalThresholds) -> list[str]:
    m = report["metrics"]
    failures = []
    if m["tie_out_rate"] < thresholds.tie_out_rate:
        failures.append(f"tie_out_rate {m['tie_out_rate']} < {thresholds.tie_out_rate}")
    if m["match"]["f1"] < thresholds.match_f1:
        failures.append(f"match_f1 {m['match']['f1']} < {thresholds.match_f1}")
    if m["classification_accuracy"] < thresholds.classification_accuracy:
        failures.append(
            f"classification_accuracy {m['classification_accuracy']} < "
            f"{thresholds.classification_accuracy}"
        )
    return failures


def thresholds_for(settings: Settings) -> EvalThresholds:
    table = load_thresholds(settings.config_dir)
    return table.get(settings.llm_provider, table["mock"])


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    th = report["thresholds"]
    status = "PASS" if not report["failures"] else "FAIL"
    lines = [
        f"# TieOut eval summary: {status}",
        "",
        f"Provider `{report['provider']}` / model `{report['model']}` / prompt "
        f"`{report['prompt_version']}` / seeds {report['seeds'][0]} to {report['seeds'][1]} "
        f"({report['runs']} runs, {report['month']}) / generated {report['generated_at']}",
        "",
        "| Metric | Result | Threshold |",
        "| --- | ---: | ---: |",
        f"| Tie-out rate | {m['tie_out_rate']:.2%} | {th['tie_out_rate']:.2%} |",
        f"| Match precision | {m['match']['precision']:.4f} | |",
        f"| Match recall | {m['match']['recall']:.4f} | |",
        f"| Match F1 | {m['match']['f1']:.4f} | {th['match_f1']:.2f} |",
        f"| Classification accuracy | {m['classification_accuracy']:.4f} | "
        f"{th['classification_accuracy']:.2f} |",
        f"| Adjusted balance accuracy | {m['adjusted_balance_accuracy']:.2%} | |",
        f"| NEEDS_REVIEW rate | {m['needs_review_rate']:.2%} | |",
        f"| LLM calls per run | {m['llm_calls_per_run']} | |",
        f"| Est. LLM cost per run (USD) | {m['cost_usd_per_run']} | |",
        f"| Avg run wall time (ms) | {m['avg_run_wall_ms']} | |",
        "",
        "## Accuracy by break type",
        "",
        "| True type | Items | Correct | Accuracy |",
        "| --- | ---: | ---: | ---: |",
    ]
    for t, row in m["per_type_accuracy"].items():
        lines.append(f"| {t} | {row['n']} | {row['correct']} | {row['accuracy']:.2%} |")
    preds = sorted({p for row in m["confusion_matrix"].values() for p in row})
    lines += [
        "",
        "## Confusion matrix (rows = truth, columns = predicted)",
        "",
        "| Truth | " + " | ".join(preds) + " |",
        "| --- |" + " ---: |" * len(preds),
    ]
    for t, row in m["confusion_matrix"].items():
        lines.append(f"| {t} | " + " | ".join(str(row.get(p, 0)) for p in preds) + " |")
    if report["failures"]:
        lines += ["", "## Threshold failures", ""] + [f"- {f}" for f in report["failures"]]
    return "\n".join(lines) + "\n"


def write_eval(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def evaluate(
    seeds: int,
    settings: Settings,
    out_dir: Path,
    seed_start: int = 1,
    month: str = "2026-09",
    progress: Callable[[int, dict[str, Any]], None] | None = None,
    provider_factory: Callable[[Settings], LLMProvider] = make_provider,
) -> tuple[dict[str, Any], Path, Path]:
    report = run_eval(seeds, settings, seed_start, month, provider_factory, progress)
    th = thresholds_for(settings)
    report["thresholds"] = th.model_dump()
    report["failures"] = check_thresholds(report, th)
    report["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    json_path, md_path = write_eval(report, out_dir)
    return report, json_path, md_path
