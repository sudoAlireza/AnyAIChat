"""Provider abstraction layer: Protocol, Capability flags, dataclasses, and error hierarchy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Any, Callable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------

class Capability(Flag):
    TEXT_CHAT = auto()
    STREAMING = auto()
    VISION = auto()
    IMAGE_GENERATION = auto()
    AUDIO_INPUT = auto()
    AUDIO_OUTPUT = auto()
    THINKING_MODE = auto()
    CODE_EXECUTION = auto()
    WEB_SEARCH = auto()
    STRUCTURED_OUTPUT = auto()
    TOOL_USE = auto()
    CONTEXT_CACHING = auto()
    FILE_UPLOAD = auto()
    EMBEDDINGS = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ModelInfo:
    id: str
    display_name: str
    provider: str
    capabilities: Capability = Capability(0)
    context_window: int = 0
    max_output: int = 0
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0


@dataclass
class ChatMessage:
    """Universal chat history format (replaces Gemini's {role, parts})."""
    role: str  # "user" | "assistant" | "system"
    content: str = ""
    images: list[bytes] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class ChatResponse:
    text: str = ""
    usage: dict = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base class for all provider errors."""
    def __init__(self, message: str = "", provider: str = "", original: Exception | None = None):
        self.provider = provider
        self.original = original
        super().__init__(message)


class RateLimitError(ProviderError):
    """API rate limit exceeded."""


class AuthenticationError(ProviderError):
    """Invalid or expired API key."""


class ModelNotFoundError(ProviderError):
    """Requested model does not exist."""


class ContentFilterError(ProviderError):
    """Content blocked by safety filters."""


class ServiceUnavailableError(ProviderError):
    """Provider is temporarily unavailable."""


class InsufficientQuotaError(ProviderError):
    """API quota exhausted."""


# ---------------------------------------------------------------------------
# Provider protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class AIProvider(Protocol):
    """Core protocol every provider must implement."""

    provider_name: str
    capabilities: Capability

    async def validate_key(self, api_key: str) -> bool:
        """Return True if the API key is valid."""
        ...

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        """Return available models for the given key."""
        ...

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Single (non-streaming) chat completion."""
        ...

    async def chat_stream(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        on_update: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Streaming chat completion. Calls *on_update* with accumulated text."""
        ...


@runtime_checkable
class ImageGenerationProvider(Protocol):
    """Optional extension for image generation."""

    async def generate_image(self, api_key: str, prompt: str, **kwargs: Any) -> Any:
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Optional extension for text embeddings."""

    async def embed(self, api_key: str, texts: list[str], model: str | None = None) -> list[list[float]]:
        ...


@runtime_checkable
class StructuredOutputProvider(Protocol):
    """Optional extension for structured (JSON schema) output."""

    async def chat_structured(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        schema: dict,
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        ...
