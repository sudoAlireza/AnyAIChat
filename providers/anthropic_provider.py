"""Anthropic provider — Claude models with extended thinking and vision."""

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


def _map_anthropic_error(exc: Exception) -> ProviderError:
    """Convert an Anthropic SDK exception to our error hierarchy."""
    msg = str(exc)
    exc_type = type(exc).__name__

    if "RateLimitError" in exc_type:
        if "quota" in msg.lower() or "insufficient" in msg.lower():
            return InsufficientQuotaError(msg, provider="anthropic", original=exc)
        return RateLimitError(msg, provider="anthropic", original=exc)
    if "AuthenticationError" in exc_type:
        return AuthenticationError(msg, provider="anthropic", original=exc)
    if "NotFoundError" in exc_type:
        return ModelNotFoundError(msg, provider="anthropic", original=exc)
    if "PermissionDeniedError" in exc_type:
        return AuthenticationError(msg, provider="anthropic", original=exc)
    if "content_filter" in msg.lower() or "ContentFilter" in exc_type:
        return ContentFilterError(msg, provider="anthropic", original=exc)
    if "overloaded" in msg.lower() or "ServiceUnavailable" in exc_type or "APIConnectionError" in exc_type:
        return ServiceUnavailableError(msg, provider="anthropic", original=exc)
    return ProviderError(msg, provider="anthropic", original=exc)


class AnthropicProvider:
    """Anthropic provider: Claude models with vision, extended thinking, structured output."""

    provider_name: str = "anthropic"
    capabilities: Capability = (
        Capability.TEXT_CHAT
        | Capability.STREAMING
        | Capability.VISION
        | Capability.THINKING_MODE
        | Capability.STRUCTURED_OUTPUT
        | Capability.TOOL_USE
    )

    def _get_client(self, api_key: str):
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=api_key)

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
            models = await client.models.list(limit=100)
            for m in models.data:
                result.append(ModelInfo(
                    id=m.id,
                    display_name=m.display_name or m.id,
                    provider="anthropic",
                ))
            result.sort(key=lambda m: m.id, reverse=True)
            return result
        except Exception as exc:
            logger.error(f"Failed to list Anthropic models: {exc}")
            raise _map_anthropic_error(exc) from exc

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = self._get_client(api_key)
        ant_messages = self._build_messages(messages)

        create_kwargs: dict[str, Any] = {
            "model": model or "claude-sonnet-4-20250514",
            "messages": ant_messages,
            "max_tokens": kwargs.get("max_tokens", 8192),
        }

        if system_instruction:
            create_kwargs["system"] = system_instruction

        # Extended thinking for Claude models
        thinking_mode = kwargs.get("thinking_mode")
        if thinking_mode and thinking_mode != "off":
            budgets = {"light": 4096, "medium": 10000, "deep": 32000}
            budget = budgets.get(thinking_mode, 10000)
            create_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
            # Extended thinking requires higher max_tokens
            create_kwargs["max_tokens"] = max(create_kwargs["max_tokens"], budget + 4096)

        try:
            response = await client.messages.create(**create_kwargs)
            metrics.increment("anthropic_messages_sent")
            return self._parse_response(response)
        except Exception as exc:
            metrics.increment("anthropic_errors")
            raise _map_anthropic_error(exc) from exc

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
        ant_messages = self._build_messages(messages)

        create_kwargs: dict[str, Any] = {
            "model": model or "claude-sonnet-4-20250514",
            "messages": ant_messages,
            "max_tokens": kwargs.get("max_tokens", 8192),
        }

        if system_instruction:
            create_kwargs["system"] = system_instruction

        thinking_mode = kwargs.get("thinking_mode")
        if thinking_mode and thinking_mode != "off":
            budgets = {"light": 4096, "medium": 10000, "deep": 32000}
            budget = budgets.get(thinking_mode, 10000)
            create_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
            create_kwargs["max_tokens"] = max(create_kwargs["max_tokens"], budget + 4096)

        start_time = time.monotonic()
        full_text = ""
        last_update_time = 0.0
        usage: dict = {}

        try:
            async with client.messages.stream(**create_kwargs) as stream:
                async for event in stream:
                    if hasattr(event, "type"):
                        if event.type == "content_block_delta":
                            if hasattr(event.delta, "text"):
                                full_text += event.delta.text
                                now = time.monotonic()
                                if on_update and now - last_update_time >= 1.5:
                                    try:
                                        await on_update(full_text)
                                    except Exception:
                                        pass
                                    last_update_time = now

                final_message = await stream.get_final_message()
                if final_message.usage:
                    usage = {
                        "prompt_tokens": final_message.usage.input_tokens or 0,
                        "completion_tokens": final_message.usage.output_tokens or 0,
                        "total_tokens": (final_message.usage.input_tokens or 0) + (final_message.usage.output_tokens or 0),
                    }

            metrics.increment("anthropic_messages_sent")
            metrics.record_latency("anthropic_streaming", time.monotonic() - start_time)
            return ChatResponse(text=full_text, usage=usage)
        except Exception as exc:
            metrics.increment("anthropic_errors")
            raise _map_anthropic_error(exc) from exc

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
        ant_messages = self._build_messages(messages)

        system = system_instruction or ""
        system += f"\n\nRespond with valid JSON matching this schema: {json.dumps(schema)}"

        try:
            response = await client.messages.create(
                model=model or "claude-sonnet-4-20250514",
                messages=ant_messages,
                system=system.strip(),
                max_tokens=kwargs.get("max_tokens", 4096),
            )
            metrics.increment("anthropic_messages_sent")
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            usage = self._extract_usage(response)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            return ChatResponse(text=text, usage=usage, metadata={"parsed": parsed})
        except Exception as exc:
            metrics.increment("anthropic_errors")
            raise _map_anthropic_error(exc) from exc

    # ----- One-shot -----

    async def one_shot(self, api_key: str, model: str, prompt: str,
                       system_instruction: str | None = None, **kwargs: Any) -> ChatResponse:
        messages = [ChatMessage(role="user", content=prompt)]
        return await self.chat(api_key, model, messages, system_instruction, **kwargs)

    # ----- Internal helpers -----

    @staticmethod
    def _build_messages(messages: list[ChatMessage]) -> list[dict]:
        ant_messages = []

        for msg in messages:
            role = "assistant" if msg.role == "assistant" else "user"

            # Handle vision (images)
            if msg.images:
                import base64
                content: list[dict] = [{"type": "text", "text": msg.content}]
                for img_bytes in msg.images:
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        }
                    })
                ant_messages.append({"role": role, "content": content})
            else:
                ant_messages.append({"role": role, "content": msg.content})

        return ant_messages

    @staticmethod
    def _parse_response(response) -> ChatResponse:
        text = ""
        thinking_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
            elif hasattr(block, "thinking"):
                thinking_text += block.thinking

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.input_tokens or 0,
                "completion_tokens": response.usage.output_tokens or 0,
                "total_tokens": (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
            }

        metadata = {}
        if thinking_text:
            metadata["thinking"] = thinking_text

        return ChatResponse(text=text or "", usage=usage, metadata=metadata)

    @staticmethod
    def _extract_usage(response) -> dict:
        if response.usage:
            return {
                "prompt_tokens": response.usage.input_tokens or 0,
                "completion_tokens": response.usage.output_tokens or 0,
                "total_tokens": (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
            }
        return {}
