"""Explicit "no LLM" provider: fails fast with actionable guidance."""

from __future__ import annotations

from .base import LLMProvider, LLMResponse, LLMError


class NoneProvider(LLMProvider):
    """Selected when no LLM is available/wanted.

    Raw transcription never touches this; any LLM task raises immediately
    with guidance instead of a confusing network error.
    """

    provider_name = "none"

    def complete(self, system: str, user: str, *, max_tokens: int = 8000,  # noqa: ARG002 - interface
                 watch=None) -> LLMResponse:  # noqa: ARG002 - interface
        raise LLMError(
            "No LLM is configured. Raw transcripts work without one — for "
            "formatted output, open Settings → LLM and either pick a provider "
            "(Ollama local/cloud, LM Studio, OpenRouter, Anthropic, OpenAI) "
            "or switch Output to \"raw\"."
        )

    def verify_credentials(self, timeout: int = 30) -> None:  # noqa: ARG002 - interface
        raise LLMError(
            "No LLM is configured. Formatted output needs one — open Settings → LLM "
            "to pick a provider, or set Output to \"raw\"."
        )

    def is_available(self) -> bool:
        return False
