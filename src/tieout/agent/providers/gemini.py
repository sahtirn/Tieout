"""Gemini provider using the official google-genai SDK with manual function calling.

* Automatic function calling is disabled: the investigator loop owns tool execution and logging.
* Function calling mode is ANY, so every turn is a tool call and the final answer arrives as the
  structured arguments of `submit_finding`, not free text.
* Model turns are replayed with their native Content so thought signatures survive multi-turn calls.
* Transient failures (429, 5xx, timeouts) are retried with exponential backoff and jitter.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from tieout.agent.providers.base import (
    LLMProvider,
    Message,
    ProviderError,
    ProviderResponse,
    ToolCall,
)
from tieout.config import Settings

RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, settings: Settings, client: Any = None, sleep: Any = time.sleep) -> None:
        from google import genai
        from google.genai import types

        if client is None and not settings.gemini_api_key:
            raise ProviderError("GEMINI_API_KEY is not set; use LLM_PROVIDER=mock or set the key")
        self.types = types
        self.model = settings.gemini_model
        self.max_retries = settings.gemini_max_retries
        self._sleep = sleep
        self.client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.gemini_timeout_s * 1000),
        )

    # ----- conversion --------------------------------------------------------------------------
    def _contents(self, messages: list[Message]) -> list[Any]:
        t = self.types
        out: list[Any] = []
        for m in messages:
            if m.role == "user":
                out.append(t.Content(role="user", parts=[t.Part.from_text(text=m.content)]))
            elif m.role == "assistant":
                if m.raw is not None:
                    out.append(m.raw)
                elif m.tool_call is not None:
                    out.append(
                        t.Content(
                            role="model",
                            parts=[
                                t.Part.from_function_call(
                                    name=m.tool_call.name, args=m.tool_call.args
                                )
                            ],
                        )
                    )
                else:
                    out.append(t.Content(role="model", parts=[t.Part.from_text(text=m.content)]))
            elif m.role == "tool" and m.tool_call is not None:
                part = t.Part.from_function_response(
                    name=m.tool_call.name, response={"result": m.tool_result or {}}
                )
                if m.tool_call.call_id and part.function_response is not None:
                    part.function_response.id = m.tool_call.call_id
                out.append(t.Content(role="user", parts=[part]))
        return out

    def _config(self, system: str, tools: list[dict[str, Any]]) -> Any:
        t = self.types
        decls = [
            t.FunctionDeclaration(
                name=s["name"], description=s["description"], parameters_json_schema=s["parameters"]
            )
            for s in tools
        ]
        return t.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=[t.Tool(function_declarations=decls)],
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )

    # ----- call --------------------------------------------------------------------------------
    @staticmethod
    def _retryable(exc: Exception) -> bool:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int):
            return code in RETRYABLE_CODES
        name = type(exc).__name__.lower()
        return "timeout" in name or "connect" in name

    def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        contents = self._contents(messages)
        config = self._config(system, tools)
        attempt = 0
        while True:
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                break
            except Exception as exc:
                if attempt >= self.max_retries or not self._retryable(exc):
                    raise ProviderError(f"gemini call failed: {exc}", retry_count=attempt) from exc
                delay = min(30.0, 2**attempt) + random.uniform(0, 0.5)
                self._sleep(delay)
                attempt += 1
        return self._parse(resp, attempt)

    def _parse(self, resp: Any, retries: int) -> ProviderResponse:
        usage = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
        out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
        out_tok += int(getattr(usage, "thoughts_token_count", 0) or 0)
        raw_content = None
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            raw_content = getattr(candidates[0], "content", None)
        calls = getattr(resp, "function_calls", None) or []
        if calls:
            fc = calls[0]
            args = dict(fc.args or {})
            return ProviderResponse(
                tool_call=ToolCall(
                    fc.name, json.loads(json.dumps(args, default=str)), getattr(fc, "id", None)
                ),
                text=None,
                input_tokens=in_tok,
                output_tokens=out_tok,
                retry_count=retries,
                raw=raw_content,
            )
        text = None
        try:
            text = resp.text
        except (ValueError, AttributeError):
            text = None
        return ProviderResponse(
            tool_call=None,
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            retry_count=retries,
            raw=raw_content,
        )
