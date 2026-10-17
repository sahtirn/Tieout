"""LLM cost estimation and telemetry aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tieout.config import AssumptionsConfig, PricingConfig
from tieout.warehouse.base import LLMCallRecord, Warehouse

MICRO = Decimal("0.000001")
MILLION = Decimal(1_000_000)


def estimate_cost(
    pricing: PricingConfig, model: str, input_tokens: int, output_tokens: int
) -> Decimal:
    price = pricing.price_for(model)
    cost = (
        Decimal(input_tokens) * price.input_per_million
        + Decimal(output_tokens) * price.output_per_million
    ) / MILLION
    return cost.quantize(MICRO, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TelemetrySummary:
    calls: int
    failed_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    avg_latency_ms: int
    total_latency_ms: int
    retries: int

    @property
    def failure_rate(self) -> float:
        return self.failed_calls / self.calls if self.calls else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "failure_rate": round(self.failure_rate, 4),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": format(self.cost_usd, "f"),
            "avg_latency_ms": self.avg_latency_ms,
            "total_latency_ms": self.total_latency_ms,
            "retries": self.retries,
        }


def summarize(calls: list[LLMCallRecord]) -> TelemetrySummary:
    total_latency = sum(c.latency_ms for c in calls)
    return TelemetrySummary(
        calls=len(calls),
        failed_calls=sum(1 for c in calls if not c.success),
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        cost_usd=sum((c.cost_usd for c in calls), Decimal("0")).quantize(MICRO),
        avg_latency_ms=total_latency // len(calls) if calls else 0,
        total_latency_ms=total_latency,
        retries=sum(c.retry_count for c in calls),
    )


@dataclass(frozen=True)
class RoiEstimate:
    minutes_saved: Decimal
    value_usd: Decimal
    llm_cost_usd: Decimal
    net_usd: Decimal
    note: str

    def as_dict(self) -> dict[str, str]:
        return {
            "analyst_minutes_saved_ASSUMPTION": format(self.minutes_saved, "f"),
            "analyst_value_usd_ASSUMPTION": format(self.value_usd, "f"),
            "llm_cost_usd": format(self.llm_cost_usd, "f"),
            "net_usd_ASSUMPTION": format(self.net_usd, "f"),
            "note": self.note,
        }


def estimate_roi(
    assumptions: AssumptionsConfig, matched_items: int, auto_resolved: int, llm_cost: Decimal
) -> RoiEstimate:
    """Minutes saved = matched items x minutes per match + auto-resolved exceptions x minutes each.

    Items routed to NEEDS_REVIEW are excluded: a human still does that work.
    """
    minutes = (
        Decimal(matched_items) * assumptions.minutes_per_matched_item
        + Decimal(auto_resolved) * assumptions.minutes_per_exception
    )
    value = (minutes / Decimal(60) * assumptions.analyst_hourly_rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return RoiEstimate(
        minutes_saved=minutes.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
        value_usd=value,
        llm_cost_usd=llm_cost,
        net_usd=(value - llm_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        note=assumptions.note,
    )


def telemetry_report(wh: Warehouse) -> dict[str, Any]:
    """Totals plus a per-run breakdown of LLM usage, cost, latency, failures, and assumed ROI."""
    runs = []
    all_calls: list[LLMCallRecord] = []
    minutes = Decimal("0")
    value = Decimal("0")
    for run in wh.list_runs():
        calls = wh.get_llm_calls(run.run_id)
        all_calls.extend(calls)
        s = summarize(calls)
        roi = (run.result or {}).get("roi", {})
        run_minutes = Decimal(roi.get("analyst_minutes_saved_ASSUMPTION", "0"))
        run_value = Decimal(roi.get("analyst_value_usd_ASSUMPTION", "0"))
        minutes += run_minutes
        value += run_value
        findings = (run.result or {}).get("summary", {}).get("findings", 0)
        runs.append(
            {
                "run_id": run.run_id,
                "started_at": run.started_at.isoformat(),
                "period": run.period,
                "status": run.status,
                "provider": run.provider,
                "model": run.model,
                "exceptions_investigated": findings,
                **s.as_dict(),
                "analyst_minutes_saved_ASSUMPTION": format(run_minutes, "f"),
            }
        )
    total = summarize(all_calls)
    return {
        "totals": {
            "runs": len(runs),
            **total.as_dict(),
            "analyst_minutes_saved_ASSUMPTION": format(minutes, "f"),
            "analyst_value_usd_ASSUMPTION": format(value, "f"),
        },
        "runs": runs,
        "assumption_note": "Minutes saved and value are estimates from config/assumptions.yaml, "
        "not measurements. LLM prices are estimates from config/pricing.yaml.",
    }
