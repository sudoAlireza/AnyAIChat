"""Provider registry — singleton mapping provider names to instances."""

from __future__ import annotations

import logging
from typing import Any

from providers.base import AIProvider, Capability

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Central registry of AI providers."""

    _instance: ProviderRegistry | None = None
    _providers: dict[str, AIProvider]

    def __new__(cls) -> ProviderRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._providers = {}
        return cls._instance

    def register(self, provider: AIProvider) -> None:
        name = provider.provider_name
        self._providers[name] = provider
        logger.info(f"Registered provider: {name}")

    def get(self, name: str) -> AIProvider | None:
        return self._providers.get(name)

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def get_all(self) -> dict[str, AIProvider]:
        return dict(self._providers)

    def register_custom(self, name: str, base_url: str) -> None:
        """Register a user-defined OpenAI-compatible endpoint.

        Lazily imports OpenAICompatProvider to avoid circular imports and
        missing dependency errors at startup.
        """
        from providers.openai_compat import OpenAICompatProvider
        provider = OpenAICompatProvider(provider_name=name, base_url=base_url)
        self.register(provider)

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for testing)."""
        cls._instance = None
