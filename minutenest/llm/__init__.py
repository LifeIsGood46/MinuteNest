"""LLM providers for transcript cleanup and document generation."""

from .base import LLMProvider, LLMError
from .ollama import OllamaProvider
from .anthropic import AnthropicProvider
from .openai import OpenAIProvider
from .openrouter import OpenRouterProvider
from .compat import OpenAICompatProvider
from .none import NoneProvider

__all__ = [
    "AnthropicProvider",
    "LLMError",
    "LLMProvider",
    "NoneProvider",
    "OllamaProvider",
    "OpenAICompatProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "get_provider",
]


def get_provider(name: str, model: str | None = None, **kwargs) -> LLMProvider:
    """Instantiate a provider by name.

    ``none``       — explicitly no LLM (raw transcripts only)
    ``ollama``     — Ollama local daemon or Ollama cloud
    ``compat``     — any OpenAI-compatible server: LM Studio, vLLM, llama.cpp…
    ``anthropic`` / ``openai`` / ``openrouter`` — hosted APIs
    """
    name = (name or "").lower()
    if name in ("none", "no_llm", "off"):
        return NoneProvider(model=model or "", **kwargs)
    if name == "ollama":
        return OllamaProvider(model=model or "llama3.1", **kwargs)
    if name == "compat":
        return OpenAICompatProvider(model=model or "", **kwargs)
    if name == "anthropic":
        return AnthropicProvider(model=model or "claude-opus-5-5", **kwargs)
    if name in ("openai", "gpt"):
        return OpenAIProvider(model=model or "gpt-5.4-mini", **kwargs)
    if name == "openrouter":
        return OpenRouterProvider(model=model or "google/gemma-4-31b-it:free", **kwargs)
    raise LLMError(f"Unknown LLM provider: {name}")
