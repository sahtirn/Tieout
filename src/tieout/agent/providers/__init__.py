"""LLM providers."""

from __future__ import annotations

from tieout.agent.providers.base import LLMProvider
from tieout.agent.providers.mock import MockProvider
from tieout.config import Settings


def make_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "gemini":
        from tieout.agent.providers.gemini import GeminiProvider

        return GeminiProvider(settings)
    return MockProvider()


__all__ = ["LLMProvider", "MockProvider", "make_provider"]
