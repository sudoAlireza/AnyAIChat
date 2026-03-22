"""OpenAI-compatible provider — works with OpenRouter, Groq, Together, and custom endpoints."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from providers.base import (
    Capability,
    ModelInfo,
    ChatMessage,
    ChatResponse,
    ProviderError,
    RateLimitError,
    AuthenticationError,
    ModelNotFoundError,
    ContentFilterError,
    ServiceUnavailableError,
    InsufficientQuotaError,
)
from monitoring.metrics import metrics

logger = logging.getLogger(__name__)


def _map_compat_error(exc: Exception, provider: str) -> ProviderError:
    """Convert an OpenAI-compatible SDK exception to our error hierarchy."""
    msg = str(exc)
    exc_type = type(exc).__name__

    if "RateLimitError" in exc_type:
        if "quota" in msg.lower() or "insufficient" in msg.lower():
            return InsufficientQuotaError(msg, provider=provider, original=exc)
        return RateLimitError(msg, provider=provider, original=exc)
    if "AuthenticationError" in exc_type:
        return AuthenticationError(msg, provider=provider, original=exc)
    if "NotFoundError" in exc_type:
        return ModelNotFoundError(msg, provider=provider, original=exc)
    if "ContentFilter" in msg or "content_filter" in msg:
        return ContentFilterError(msg, provider=provider, original=exc)
    if "ServiceUnavailableError" in exc_type or "APIConnectionError" in exc_type:
        return ServiceUnavailableError(msg, provider=provider, original=exc)
    return ProviderError(msg, provider=provider, original=exc)


# Pre-configured endpoints for popular services
KNOWN_ENDPOINTS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "display_name": "OpenRouter",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "display_name": "Groq",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "display_name": "Together AI",
    },
}


class OpenAICompatProvider:
    """Generic OpenAI-compatible provider for third-party endpoints."""

    default_model: str = ""
    capabilities: Capability = (
        Capability.TEXT_CHAT
        | Capability.STREAMING
        | Capability.VISION
        | Capability.STRUCTURED_OUTPUT
    )

    def __init__(self, provider_name: str = "openai_compat", base_url: str = "", display_name: str = ""):
        self.provider_name = provider_name
        self.base_url = base_url
        self.display_name = display_name or provider_name

        # Apply known endpoint defaults
        if provider_name in KNOWN_ENDPOINTS and not base_url:
            endpoint = KNOWN_ENDPOINTS[provider_name]
            self.base_url = endpoint["base_url"]
            self.display_name = endpoint["display_name"]

    def _get_client(self, api_key: str):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key, base_url=self.base_url)

    async def validate_key(self, api_key: str) -> bool:
        try:
            models = await self.list_models(api_key)
            return len(models) > 0
        except Exception:
            return False

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        try:
            client = self._get_client(api_key)
            result: list[ModelInfo] = []
            models = await client.models.list()
            for m in models.data:
                result.append(ModelInfo(
                    id=m.id,
                    display_name=m.id,
                    provider=self.provider_name,
                ))
            result.sort(key=lambda m: m.id, reverse=True)
            return result
        except Exception as exc:
            logger.error(f"Failed to list models from {self.provider_name}: {exc}")
            raise _map_compat_error(exc, self.provider_name) from exc

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = self._get_client(api_key)
        oai_messages = self._build_messages(messages, system_instruction)

        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
        }

        try:
            response = await client.chat.completions.create(**create_kwargs)
            metrics.increment(f"{self.provider_name}_messages_sent")
            return self._parse_response(response)
        except Exception as exc:
            metrics.increment(f"{self.provider_name}_errors")
            raise _map_compat_error(exc, self.provider_name) from exc

    async def chat_stream(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        on_update: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = self._get_client(api_key)
        oai_messages = self._build_messages(messages, system_instruction)

        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "stream": True,
        }

        start_time = time.monotonic()
        full_text = ""
        last_update_time = 0.0
        usage: dict = {}

        try:
            stream = await client.chat.completions.create(**create_kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    full_text += chunk.choices[0].delta.content
                    now = time.monotonic()
                    if on_update and now - last_update_time >= 1.5:
                        try:
                            await on_update(full_text)
                        except Exception:
                            pass
                        last_update_time = now
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }

            metrics.increment(f"{self.provider_name}_messages_sent")
            metrics.record_latency(f"{self.provider_name}_streaming", time.monotonic() - start_time)
            return ChatResponse(text=full_text, usage=usage)
        except Exception as exc:
            metrics.increment(f"{self.provider_name}_errors")
            raise _map_compat_error(exc, self.provider_name) from exc

    # ----- StructuredOutputProvider -----

    async def chat_structured(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        schema: dict,
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = self._get_client(api_key)
        oai_messages = self._build_messages(messages, system_instruction)

        # Add JSON instruction to system message
        if oai_messages and oai_messages[0]["role"] == "system":
            oai_messages[0]["content"] += f"\n\nRespond with valid JSON matching this schema: {json.dumps(schema)}"
        else:
            oai_messages.insert(0, {
                "role": "system",
                "content": f"Respond with valid JSON matching this schema: {json.dumps(schema)}"
            })

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=oai_messages,
                response_format={"type": "json_object"},
            )
            metrics.increment(f"{self.provider_name}_messages_sent")
            text = response.choices[0].message.content or ""
            usage = self._extract_usage(response)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            return ChatResponse(text=text, usage=usage, metadata={"parsed": parsed})
        except Exception as exc:
            metrics.increment(f"{self.provider_name}_errors")
            raise _map_compat_error(exc, self.provider_name) from exc

    # ----- One-shot -----

    async def one_shot(self, api_key: str, model: str, prompt: str,
                       system_instruction: str | None = None, **kwargs: Any) -> ChatResponse:
        messages = [ChatMessage(role="user", content=prompt)]
        return await self.chat(api_key, model, messages, system_instruction, **kwargs)

    # ----- Internal helpers -----

    @staticmethod
    def _build_messages(messages: list[ChatMessage], system_instruction: str | None = None) -> list[dict]:
        oai_messages = []
        if system_instruction:
            oai_messages.append({"role": "system", "content": system_instruction})

        for msg in messages:
            role = msg.role
            if role not in ("assistant", "system"):
                role = "user"

            if msg.images:
                import base64
                content: list[dict] = [{"type": "text", "text": msg.content}]
                for img_bytes in msg.images:
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                    })
                oai_messages.append({"role": role, "content": content})
            else:
                oai_messages.append({"role": role, "content": msg.content})

        return oai_messages

    @staticmethod
    def _parse_response(response) -> ChatResponse:
        choice = response.choices[0] if response.choices else None
        text = choice.message.content if choice else ""
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        return ChatResponse(text=text or "", usage=usage)

    @staticmethod
    def _extract_usage(response) -> dict:
        if response.usage:
            return {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        return {}
