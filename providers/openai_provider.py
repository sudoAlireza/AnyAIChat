"""OpenAI provider — GPT-4o, o3, DALL-E, Whisper, structured output, web search."""

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


def _map_openai_error(exc: Exception) -> ProviderError:
    """Convert an OpenAI SDK exception to our error hierarchy."""
    msg = str(exc)
    exc_type = type(exc).__name__

    if "RateLimitError" in exc_type:
        if "quota" in msg.lower() or "insufficient" in msg.lower():
            return InsufficientQuotaError(msg, provider="openai", original=exc)
        return RateLimitError(msg, provider="openai", original=exc)
    if "AuthenticationError" in exc_type:
        return AuthenticationError(msg, provider="openai", original=exc)
    if "NotFoundError" in exc_type:
        return ModelNotFoundError(msg, provider="openai", original=exc)
    if "ContentFilter" in msg or "content_filter" in msg:
        return ContentFilterError(msg, provider="openai", original=exc)
    if "ServiceUnavailableError" in exc_type or "APIConnectionError" in exc_type:
        return ServiceUnavailableError(msg, provider="openai", original=exc)
    return ProviderError(msg, provider="openai", original=exc)


class OpenAIProvider:
    """OpenAI provider: GPT-4o, o3, DALL-E, structured output, web search."""

    provider_name: str = "openai"
    capabilities: Capability = (
        Capability.TEXT_CHAT
        | Capability.STREAMING
        | Capability.VISION
        | Capability.IMAGE_GENERATION
        | Capability.AUDIO_INPUT
        | Capability.AUDIO_OUTPUT
        | Capability.THINKING_MODE
        | Capability.STRUCTURED_OUTPUT
        | Capability.TOOL_USE
        | Capability.WEB_SEARCH
    )

    def _get_client(self, api_key: str):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key)

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
                if any(p in m.id for p in ("gpt-", "o1", "o3", "o4")):
                    result.append(ModelInfo(
                        id=m.id,
                        display_name=m.id,
                        provider="openai",
                    ))
            result.sort(key=lambda m: m.id, reverse=True)
            return result
        except Exception as exc:
            logger.error(f"Failed to list OpenAI models: {exc}")
            raise _map_openai_error(exc) from exc

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
            "model": model or "gpt-4o",
            "messages": oai_messages,
        }

        # Thinking mode for o-series models
        thinking_mode = kwargs.get("thinking_mode")
        if thinking_mode and thinking_mode != "off" and model and ("o1" in model or "o3" in model or "o4" in model):
            budgets = {"light": "low", "medium": "medium", "deep": "high"}
            create_kwargs["reasoning_effort"] = budgets.get(thinking_mode, "medium")

        # Web search
        if kwargs.get("web_search"):
            create_kwargs["tools"] = [{"type": "web_search_preview"}]

        try:
            response = await client.chat.completions.create(**create_kwargs)
            metrics.increment("openai_messages_sent")
            return self._parse_response(response)
        except Exception as exc:
            metrics.increment("openai_errors")
            raise _map_openai_error(exc) from exc

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
            "model": model or "gpt-4o",
            "messages": oai_messages,
            "stream": True,
        }

        thinking_mode = kwargs.get("thinking_mode")
        if thinking_mode and thinking_mode != "off" and model and ("o1" in model or "o3" in model or "o4" in model):
            budgets = {"light": "low", "medium": "medium", "deep": "high"}
            create_kwargs["reasoning_effort"] = budgets.get(thinking_mode, "medium")

        if kwargs.get("web_search"):
            create_kwargs["tools"] = [{"type": "web_search_preview"}]

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

            metrics.increment("openai_messages_sent")
            metrics.record_latency("openai_streaming", time.monotonic() - start_time)
            return ChatResponse(text=full_text, usage=usage)
        except Exception as exc:
            metrics.increment("openai_errors")
            raise _map_openai_error(exc) from exc

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
                model=model or "gpt-4o",
                messages=oai_messages,
                response_format={"type": "json_object"},
            )
            metrics.increment("openai_messages_sent")
            text = response.choices[0].message.content or ""
            usage = self._extract_usage(response)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            return ChatResponse(text=text, usage=usage, metadata={"parsed": parsed})
        except Exception as exc:
            metrics.increment("openai_errors")
            raise _map_openai_error(exc) from exc

    # ----- ImageGenerationProvider -----

    async def generate_image(self, api_key: str, prompt: str, **kwargs: Any) -> Any:
        client = self._get_client(api_key)
        try:
            response = await client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard",
                n=1,
            )
            return response
        except Exception as exc:
            raise _map_openai_error(exc) from exc

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
            if role == "assistant":
                role = "assistant"
            elif role == "system":
                role = "system"
            else:
                role = "user"

            # Handle vision (images)
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
