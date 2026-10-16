"""Deterministic provider. Runs the rule-based policy through the real tool-calling protocol."""

from __future__ import annotations

import json
from typing import Any

from tieout.agent.policy import ExceptionContext, NextCall, Observation, decide
from tieout.agent.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderResponse,
    ToolCall,
    estimate_tokens,
)


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "mock") -> None:
        self.model = model

    def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        context = next((m.data for m in messages if m.role == "user" and m.data), None)
        if context is None:
            raise ProviderError("mock provider needs the exception context message")
        ctx = ExceptionContext.from_dict(context)
        observations = [
            Observation(m.tool_call.name, m.tool_call.args, m.tool_result or {})
            for m in messages
            if m.role == "tool" and m.tool_call is not None and m.tool_call.name != "submit_finding"
        ]
        decision = decide(ctx, observations)
        if isinstance(decision, NextCall):
            call = ToolCall(decision.tool, decision.args)
        else:
            call = ToolCall("submit_finding", decision)
        prompt_text = (
            system
            + json.dumps(tools)
            + "".join(m.content + json.dumps(m.tool_result or {}) for m in messages)
        )
        return ProviderResponse(
            tool_call=call,
            text=None,
            input_tokens=estimate_tokens(prompt_text),
            output_tokens=estimate_tokens(json.dumps(call.args)),
        )
