"""Provider-neutral LLM interface used by the investigator loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    call_id: str | None = None


@dataclass
class Message:
    """One conversation turn.

    role: "user" (instructions or exception context), "assistant" (a model turn, usually a tool
    call), or "tool" (a tool result). `data` carries structured context the mock policy can read;
    `raw` carries provider-native content so it can be replayed verbatim (for example Gemini
    thought signatures).
    """

    role: str
    content: str = ""
    tool_call: ToolCall | None = None
    tool_result: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    raw: Any = None


@dataclass
class ProviderResponse:
    tool_call: ToolCall | None
    text: str | None
    input_tokens: int
    output_tokens: int
    retry_count: int = 0
    raw: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    """The provider could not produce a response after its own retries."""

    def __init__(self, message: str, retry_count: int = 0) -> None:
        super().__init__(message)
        self.retry_count = retry_count


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def complete(
        self, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> ProviderResponse:
        """Return either a tool call (including submit_finding) or text."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (about 4 characters per token) for providers without usage data."""
    return max(1, (len(text) + 3) // 4)
