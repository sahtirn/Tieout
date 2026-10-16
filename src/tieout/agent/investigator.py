"""The investigation loop: provider <-> tools, validated final answer, retry, fallback.

For each exception:
1. The provider sees the system prompt, the exception context, and prior tool results only.
2. It returns a tool call; the tool runs (read-only) and the result is appended. Repeat.
3. A `submit_finding` call is validated by deterministic controls. One invalid answer gets one
   retry with the validation error fed back. A second failure, a provider error, or hitting
   MAX_AGENT_STEPS falls back to the deterministic policy, and the item is flagged NEEDS_REVIEW.
4. Valid findings below REVIEW_CONFIDENCE_THRESHOLD are flagged NEEDS_REVIEW.
Every provider call is logged to llm_calls and every step to agent_steps.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tieout.agent.policy import ExceptionContext, NextCall, Observation, decide
from tieout.agent.providers.base import LLMProvider, Message, ProviderError, ToolCall
from tieout.agent.tools import TOOL_SPECS, LedgerView, ToolBox
from tieout.agent.validation import FindingValidationError, validate_finding
from tieout.config import PricingConfig, Settings
from tieout.models import (
    PAIRED_BREAKS,
    AgentFinding,
    BreakType,
    FindingSource,
    ReconException,
    ResolvedFinding,
    ReviewStatus,
)
from tieout.telemetry import estimate_cost
from tieout.warehouse.base import AgentStepRecord, LLMCallRecord, Warehouse

SUBMIT = "submit_finding"
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def load_prompt(prompts_dir: str, version: str) -> str:
    path = Path(prompts_dir) / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt version {version!r} not found at {path}")
    return path.read_text(encoding="utf-8")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Investigator:
    def __init__(
        self,
        warehouse: Warehouse,
        settings: Settings,
        provider: LLMProvider,
        view: LedgerView,
        pricing: PricingConfig,
        run_id: str,
    ) -> None:
        self.wh = warehouse
        self.settings = settings
        self.provider = provider
        self.view = view
        self.tools = ToolBox(view)
        self.pricing = pricing
        self.run_id = run_id
        self.prompt_version = settings.prompt_version
        self.system_prompt = load_prompt(settings.prompts_dir, settings.prompt_version)

    # ----- public ------------------------------------------------------------------------------
    def investigate_all(self, exceptions: list[ReconException]) -> list[ResolvedFinding]:
        """Bank items first. Items already explained as a counterpart of a finding are skipped."""
        findings: list[ResolvedFinding] = []
        for exc in exceptions:
            if exc.item_id in self.view.resolved:
                continue
            rf = self.investigate(exc)
            self.wh.save_finding(self.run_id, rf)
            findings.append(rf)
            self.view.resolved[exc.item_id] = exc.exception_id
            cp = rf.finding.counterpart_id
            if cp and rf.finding.break_type in PAIRED_BREAKS:
                self.view.resolved[cp] = exc.exception_id
        return findings

    def context_for(self, exc: ReconException) -> ExceptionContext:
        return ExceptionContext(
            exception_id=exc.exception_id,
            ledger=exc.side.value,
            item_id=exc.item_id,
            item_date=exc.item_date,
            amount=exc.amount,
            description=exc.description,
            period_start=self.view.period_start,
            period_end=self.view.period_end,
            window_days=self.view.window_days,
        )

    def investigate(self, exc: ReconException) -> ResolvedFinding:
        ctx = self.context_for(exc)
        messages = [
            Message(
                role="user",
                content="Investigate this unreconciled item:\n"
                + json.dumps(ctx.as_dict(), indent=2),
                data=ctx.as_dict(),
            )
        ]
        step = 0
        retried = False
        provider_calls = 0
        while provider_calls < self.settings.max_agent_steps:
            provider_calls += 1
            step += 1
            started = time.perf_counter()
            try:
                resp = self.provider.complete(self.system_prompt, messages, TOOL_SPECS)
            except ProviderError as err:
                self._log_call(
                    exc, started, 0, 0, success=False, retries=err.retry_count, error=str(err)
                )
                self._log_step(exc, step, "provider_error", None, None, {"error": str(err)})
                return self._fallback(exc, ctx, step, f"provider error: {err}")
            self._log_call(
                exc,
                started,
                resp.input_tokens,
                resp.output_tokens,
                success=True,
                retries=resp.retry_count + (1 if retried else 0),
                error=None,
            )
            call = resp.tool_call
            if call is not None and call.name != SUBMIT:
                result = self.tools.execute(call.name, call.args)
                self._log_step(exc, step, "tool_call", call.name, call.args, result)
                messages.append(Message(role="assistant", tool_call=call, raw=resp.raw))
                messages.append(Message(role="tool", tool_call=call, tool_result=result))
                continue
            payload = call.args if call is not None else _parse_text(resp.text)
            try:
                finding = validate_finding(payload, ctx, self.view)
            except FindingValidationError as err:
                self._log_step(exc, step, "invalid_finding", SUBMIT, payload, {"error": str(err)})
                if retried:
                    return self._fallback(
                        exc, ctx, step, f"finding failed validation after one retry: {err}"
                    )
                retried = True
                answered = call or ToolCall(SUBMIT, {})
                messages.append(Message(role="assistant", tool_call=answered, raw=resp.raw))
                messages.append(
                    Message(
                        role="tool",
                        tool_call=answered,
                        tool_result={
                            "accepted": False,
                            "error": str(err),
                            "instruction": "Fix these problems and call submit_finding again.",
                        },
                    )
                )
                continue
            status, reasons = self._review_status(finding)
            self._log_step(
                exc,
                step,
                "final",
                SUBMIT,
                finding.model_dump(mode="json"),
                {"accepted": True, "review_status": status.value},
            )
            return ResolvedFinding(
                exception_id=exc.exception_id,
                item_id=exc.item_id,
                side=exc.side,
                finding=finding,
                source=FindingSource.AGENT,
                review_status=status,
                review_reasons=reasons,
                prompt_version=self.prompt_version,
                steps_used=step,
            )
        return self._fallback(
            exc, ctx, step, f"step limit of {self.settings.max_agent_steps} reached"
        )

    # ----- internals ---------------------------------------------------------------------------
    def _review_status(self, finding: AgentFinding) -> tuple[ReviewStatus, list[str]]:
        reasons = []
        if finding.break_type == BreakType.UNIDENTIFIED:
            reasons.append("agent could not identify the cause")
        if finding.confidence < self.settings.review_confidence_threshold:
            reasons.append(
                f"confidence {finding.confidence:.2f} below threshold "
                f"{self.settings.review_confidence_threshold:.2f}"
            )
        return (ReviewStatus.NEEDS_REVIEW if reasons else ReviewStatus.AUTO_ACCEPTED), reasons

    def _fallback(
        self, exc: ReconException, ctx: ExceptionContext, step: int, reason: str
    ) -> ResolvedFinding:
        """Deterministic classifier: same policy, tools called directly, always NEEDS_REVIEW."""
        observations: list[Observation] = []
        for _ in range(12):
            decision = decide(ctx, observations)
            if not isinstance(decision, NextCall):
                break
            step += 1
            result = self.tools.execute(decision.tool, decision.args)
            self._log_step(exc, step, "fallback_tool_call", decision.tool, decision.args, result)
            observations.append(Observation(decision.tool, decision.args, result))
        else:  # pragma: no cover - the policy always terminates within a handful of calls
            raise RuntimeError("fallback policy did not terminate")
        try:
            finding = validate_finding(decision, ctx, self.view)
        except FindingValidationError as err:
            finding = AgentFinding(
                break_type=BreakType.UNIDENTIFIED,
                explanation=f"The automated investigation of {exc.item_id} was inconclusive. "
                "A reviewer must classify it from source documents.",
                evidence_ids=[exc.item_id],
                proposed_action="Manual investigation required.",
                confidence=0.0,
            )
            reason += f"; fallback finding also failed validation: {err}"
        step += 1
        self._log_step(
            exc, step, "fallback_final", SUBMIT, finding.model_dump(mode="json"), {"reason": reason}
        )
        return ResolvedFinding(
            exception_id=exc.exception_id,
            item_id=exc.item_id,
            side=exc.side,
            finding=finding,
            source=FindingSource.FALLBACK,
            review_status=ReviewStatus.NEEDS_REVIEW,
            review_reasons=[reason],
            prompt_version=self.prompt_version,
            steps_used=step,
        )

    def _log_step(
        self,
        exc: ReconException,
        step: int,
        kind: str,
        tool: str | None,
        args: dict[str, Any] | None,
        result: Any,
    ) -> None:
        self.wh.log_agent_step(
            AgentStepRecord(
                run_id=self.run_id,
                exception_id=exc.exception_id,
                step_no=step,
                step_type=kind,
                tool_name=tool,
                arguments=args,
                result=result,
                created_at=_now(),
            )
        )

    def _log_call(
        self,
        exc: ReconException,
        started: float,
        in_tok: int,
        out_tok: int,
        *,
        success: bool,
        retries: int,
        error: str | None,
    ) -> None:
        self.wh.log_llm_call(
            LLMCallRecord(
                call_id=str(uuid.uuid4()),
                run_id=self.run_id,
                exception_id=exc.exception_id,
                created_at=_now(),
                provider=self.provider.name,
                model=self.provider.model,
                prompt_version=self.prompt_version,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=int((time.perf_counter() - started) * 1000),
                cost_usd=estimate_cost(self.pricing, self.provider.model, in_tok, out_tok),
                success=success,
                retry_count=retries,
                error=error,
            )
        )


def _parse_text(text: str | None) -> dict[str, Any]:
    """Lenient fallback for providers that answer in text: extract the first JSON object."""
    if not text:
        return {}
    m = _JSON_BLOCK.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
