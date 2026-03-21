"""Gemini provider — refactored from core.py to implement AIProvider protocol."""

from __future__ import annotations

import json
import os
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from google import genai
from google.genai import types
from google.api_core.exceptions import (
    ResourceExhausted,
    ServiceUnavailable,
    InvalidArgument,
    PermissionDenied,
    NotFound,
)
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import (
    GEMINI_MODEL,
    MAX_HISTORY_MESSAGES,
    SAFETY_OVERRIDE,
    CACHE_TTL_SECONDS,
    CACHE_MIN_TOKENS,
    RAG_TOP_K,
)
from providers.base import (
    AIProvider,
    ImageGenerationProvider,
    EmbeddingProvider,
    StructuredOutputProvider,
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


def _map_gemini_error(exc: Exception) -> ProviderError:
    """Convert a Google API exception to our error hierarchy."""
    msg = str(exc)
    if isinstance(exc, ResourceExhausted):
        if "quota" in msg.lower():
            return InsufficientQuotaError(msg, provider="gemini", original=exc)
        return RateLimitError(msg, provider="gemini", original=exc)
    if isinstance(exc, PermissionDenied):
        return AuthenticationError(msg, provider="gemini", original=exc)
    if isinstance(exc, NotFound):
        return ModelNotFoundError(msg, provider="gemini", original=exc)
    if isinstance(exc, InvalidArgument) and "safety" in msg.lower():
        return ContentFilterError(msg, provider="gemini", original=exc)
    if isinstance(exc, ServiceUnavailable):
        return ServiceUnavailableError(msg, provider="gemini", original=exc)
    return ProviderError(msg, provider="gemini", original=exc)


class GeminiProvider:
    """Google Gemini provider implementing AIProvider + optional extension protocols."""

    provider_name: str = "gemini"
    default_model: str = "gemini-2.0-flash"
    capabilities: Capability = (
        Capability.TEXT_CHAT
        | Capability.STREAMING
        | Capability.VISION
        | Capability.IMAGE_GENERATION
        | Capability.AUDIO_INPUT
        | Capability.AUDIO_OUTPUT
        | Capability.THINKING_MODE
        | Capability.CODE_EXECUTION
        | Capability.WEB_SEARCH
        | Capability.STRUCTURED_OUTPUT
        | Capability.TOOL_USE
        | Capability.CONTEXT_CACHING
        | Capability.FILE_UPLOAD
        | Capability.EMBEDDINGS
    )

    def __init__(self) -> None:
        self._safety_settings = self._load_safety_settings()

    # ----- safety settings -----

    @staticmethod
    def _load_safety_settings() -> list[types.SafetySetting]:
        settings_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "safety_settings.json")
        try:
            with open(settings_path, "r") as fp:
                raw = json.load(fp)
        except FileNotFoundError:
            return []

        if SAFETY_OVERRIDE == "BLOCK_NONE":
            for s in raw:
                s["threshold"] = "BLOCK_NONE"

        return [types.SafetySetting(category=s["category"], threshold=s["threshold"]) for s in raw]

    # ----- AIProvider protocol -----

    async def validate_key(self, api_key: str) -> bool:
        try:
            models = await self.list_models(api_key)
            return len(models) > 0
        except Exception:
            return False

    async def list_models(self, api_key: str) -> list[ModelInfo]:
        try:
            client = genai.Client(api_key=api_key)
            models: list[ModelInfo] = []
            async for m in await client.aio.models.list():
                models.append(ModelInfo(
                    id=m.name,
                    display_name=getattr(m, "display_name", m.name),
                    provider="gemini",
                ))
            return models
        except Exception as exc:
            logger.error(f"Failed to list Gemini models: {exc}")
            raise _map_gemini_error(exc) from exc

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = genai.Client(api_key=api_key)
        config = self._build_config(
            system_instruction=system_instruction,
            thinking_mode=kwargs.get("thinking_mode"),
            code_execution=kwargs.get("code_execution", False),
            web_search=kwargs.get("web_search", False),
            cached_content=kwargs.get("cached_content"),
            response_schema=kwargs.get("response_schema"),
        )
        history = self._messages_to_contents(messages[:-1]) if len(messages) > 1 else []
        if len(history) > MAX_HISTORY_MESSAGES * 2:
            history = history[-(MAX_HISTORY_MESSAGES * 2):]

        user_content = self._message_to_content_parts(messages[-1]) if messages else [""]

        try:
            chat_session = client.aio.chats.create(
                model=model,
                config=config,
                history=history if history else None,
            )
            response = await chat_session.send_message(user_content)
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            sources = self._extract_grounding_metadata(response)
            return ChatResponse(text=response.text or "", usage=usage, sources=sources)
        except (ResourceExhausted, ServiceUnavailable, PermissionDenied, NotFound, InvalidArgument) as exc:
            raise _map_gemini_error(exc) from exc
        except Exception as exc:
            logger.error(f"Gemini chat error: {exc}")
            metrics.increment("gemini_errors")
            raise _map_gemini_error(exc) from exc

    async def chat_stream(
        self,
        api_key: str,
        model: str,
        messages: list[ChatMessage],
        system_instruction: str | None = None,
        on_update: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        client = genai.Client(api_key=api_key)
        config = self._build_config(
            system_instruction=system_instruction,
            thinking_mode=kwargs.get("thinking_mode"),
            code_execution=kwargs.get("code_execution", False),
            web_search=kwargs.get("web_search", False),
            cached_content=kwargs.get("cached_content"),
        )
        history = self._messages_to_contents(messages[:-1]) if len(messages) > 1 else []
        if len(history) > MAX_HISTORY_MESSAGES * 2:
            history = history[-(MAX_HISTORY_MESSAGES * 2):]

        user_content = self._message_to_content_parts(messages[-1]) if messages else [""]

        start_time = time.monotonic()
        full_text = ""
        last_update_time = 0.0
        usage: dict = {}
        last_response = None

        try:
            chat_session = client.aio.chats.create(
                model=model,
                config=config,
                history=history if history else None,
            )
            async for chunk in await chat_session.send_message_stream(user_content):
                last_response = chunk
                if hasattr(chunk, "text") and chunk.text:
                    full_text += chunk.text
                    now = time.monotonic()
                    if on_update and now - last_update_time >= 1.5:
                        try:
                            await on_update(full_text)
                        except Exception:
                            pass
                        last_update_time = now
                chunk_usage = self._extract_usage(chunk)
                if chunk_usage:
                    usage = chunk_usage

            sources = self._extract_grounding_metadata(last_response) if last_response else []
            metrics.increment("gemini_messages_sent")
            metrics.record_latency("gemini_send_message_streaming", time.monotonic() - start_time)
            return ChatResponse(text=full_text, usage=usage, sources=sources)
        except (ResourceExhausted, ServiceUnavailable, PermissionDenied, NotFound, InvalidArgument) as exc:
            raise _map_gemini_error(exc) from exc
        except Exception as exc:
            logger.error(f"Gemini streaming error: {exc}")
            metrics.increment("gemini_errors")
            raise _map_gemini_error(exc) from exc

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
        client = genai.Client(api_key=api_key)
        config = self._build_config(
            system_instruction=system_instruction,
            response_schema=schema,
        )
        prompt = messages[-1].content if messages else ""
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            parsed = json.loads(response.text)
            return ChatResponse(text=response.text, usage=usage, metadata={"parsed": parsed})
        except (ResourceExhausted, ServiceUnavailable, PermissionDenied, NotFound, InvalidArgument) as exc:
            raise _map_gemini_error(exc) from exc
        except json.JSONDecodeError as exc:
            logger.error(f"Failed to parse structured response: {exc}")
            return ChatResponse(text="", metadata={"error": "json_parse_error"})
        except Exception as exc:
            logger.error(f"Gemini structured error: {exc}")
            metrics.increment("gemini_errors")
            raise _map_gemini_error(exc) from exc

    # ----- ImageGenerationProvider -----

    async def generate_image(self, api_key: str, prompt: str, **kwargs: Any) -> Any:
        client = genai.Client(api_key=api_key)
        try:
            response = await client.aio.models.generate_images(
                model="imagen-3.0-generate-001",
                prompt=prompt,
                config=types.GenerateImagesConfig(number_of_images=1),
            )
            return response
        except Exception as exc:
            logger.error(f"Failed to generate image: {exc}")
            raise _map_gemini_error(exc) from exc

    # ----- EmbeddingProvider -----

    async def embed(self, api_key: str, texts: list[str], model: str | None = None) -> list[list[float]]:
        from config import EMBEDDING_MODEL
        client = genai.Client(api_key=api_key)
        embed_model = model or EMBEDDING_MODEL
        embeddings: list[list[float]] = []
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            try:
                response = await client.aio.models.embed_content(
                    model=embed_model,
                    contents=batch,
                )
                for emb in response.embeddings:
                    embeddings.append(list(emb.values))
            except Exception as e:
                logger.error(f"Failed to embed batch {i}: {e}")
                for _ in batch:
                    embeddings.append([0.0] * 768)
        return embeddings

    # ----- Gemini-specific extras (used by ChatSession) -----

    async def one_shot(self, api_key: str, model: str, prompt: str,
                       system_instruction: str | None = None, **kwargs: Any) -> ChatResponse:
        """Single prompt→response with no chat session."""
        client = genai.Client(api_key=api_key)
        config = self._build_config(
            system_instruction=system_instruction,
            thinking_mode=kwargs.get("thinking_mode"),
            code_execution=kwargs.get("code_execution", False),
            web_search=kwargs.get("web_search", False),
        )
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=prompt, config=config,
            )
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            return ChatResponse(text=response.text or "", usage=usage)
        except (ResourceExhausted, ServiceUnavailable, PermissionDenied, NotFound, InvalidArgument) as exc:
            raise _map_gemini_error(exc) from exc
        except Exception as exc:
            logger.error(f"Gemini one_shot error: {exc}")
            metrics.increment("gemini_errors")
            raise _map_gemini_error(exc) from exc

    async def upload_file(self, api_key: str, file_path: str, mime_type: str) -> Any:
        client = genai.Client(api_key=api_key)
        return await client.aio.files.upload(
            file=file_path, config=types.UploadFileConfig(mime_type=mime_type)
        )

    async def generate_content_with_file(self, api_key: str, model: str, prompt: str, uploaded_file: Any) -> str:
        client = genai.Client(api_key=api_key)
        config = self._build_config()
        response = await client.aio.models.generate_content(
            model=model, contents=[prompt, uploaded_file], config=config,
        )
        return (response.text or "").strip()

    async def list_uploaded_files(self, api_key: str) -> list[dict]:
        try:
            client = genai.Client(api_key=api_key)
            files = []
            async for f in await client.aio.files.list():
                files.append({
                    "name": f.name,
                    "display_name": getattr(f, "display_name", ""),
                    "uri": getattr(f, "uri", ""),
                    "create_time": str(getattr(f, "create_time", "")),
                    "mime_type": getattr(f, "mime_type", ""),
                    "size_bytes": getattr(f, "size_bytes", 0),
                })
            return files
        except Exception as exc:
            logger.error(f"Failed to list uploaded files: {exc}")
            return []

    async def create_or_get_cache(
        self,
        api_key: str,
        model: str,
        pool,
        user_id: int,
        system_instruction: str,
        knowledge_docs: list[dict],
    ) -> str | None:
        """Create or retrieve a context cache for the current user."""
        if not pool or not user_id:
            return None

        try:
            from database.database import get_active_cache, save_cache_record, delete_cache_record

            client = genai.Client(api_key=api_key)
            existing = await get_active_cache(pool, user_id, model)
            if existing:
                try:
                    cache = await client.aio.caches.get(name=existing["cache_name"])
                    if cache:
                        logger.info(f"Reusing existing cache: {existing['cache_name']}")
                        return existing["cache_name"]
                except Exception:
                    logger.info("Cached content expired in API, will recreate")
                    await delete_cache_record(pool, existing["cache_name"])

            cache_parts = [system_instruction]
            for doc in knowledge_docs:
                if doc.get("full_content"):
                    cache_parts.append(f"\n\n--- Document: {doc['file_name']} ---\n{doc['full_content']}")

            full_content = "\n".join(cache_parts)
            estimated_tokens = len(full_content) // 4

            if estimated_tokens < CACHE_MIN_TOKENS:
                logger.info(f"Content too small for caching ({estimated_tokens} est. tokens < {CACHE_MIN_TOKENS})")
                return None

            cache = await client.aio.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    contents=[types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=full_content)],
                    )],
                    ttl=f"{CACHE_TTL_SECONDS}s",
                    display_name=f"user_{user_id}_{int(time.time())}",
                ),
            )

            cache_name = cache.name
            token_count = getattr(getattr(cache, "usage_metadata", None), "total_token_count", estimated_tokens)
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=CACHE_TTL_SECONDS)).isoformat()

            await save_cache_record(pool, user_id, cache_name, model, token_count, expires_at)
            logger.info(f"Created new cache: {cache_name} ({token_count} tokens)")
            return cache_name

        except Exception as e:
            logger.warning(f"Failed to create/get cache: {e}")
            return None

    # ----- Internal helpers -----

    def _build_config(
        self,
        system_instruction: str | None = None,
        thinking_mode: str | None = None,
        code_execution: bool = False,
        web_search: bool = False,
        cached_content: str | None = None,
        response_schema: dict | None = None,
    ) -> types.GenerateContentConfig:
        tool_list = []
        if web_search:
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))
        if code_execution:
            tool_list.append(types.Tool(code_execution=types.ToolCodeExecution()))

        kwargs: dict[str, Any] = {
            "system_instruction": system_instruction,
            "safety_settings": self._safety_settings,
            "tools": tool_list if tool_list else None,
        }

        if response_schema:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema

        if cached_content:
            kwargs["cached_content"] = cached_content

        if thinking_mode and thinking_mode != "off":
            budgets = {"light": 1024, "medium": 4096, "deep": 8192}
            budget = budgets.get(thinking_mode, 0)
            if budget > 0:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)

        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _messages_to_contents(messages: list[ChatMessage]) -> list[types.Content]:
        """Convert ChatMessage list to Gemini Content list."""
        result = []
        for msg in messages:
            role = "model" if msg.role == "assistant" else msg.role
            parts = []
            if msg.content:
                parts.append(types.Part.from_text(text=msg.content))
            if parts:
                result.append(types.Content(role=role, parts=parts))
        return result

    @staticmethod
    def _message_to_content_parts(msg: ChatMessage) -> list:
        """Convert single ChatMessage to content parts list for send_message."""
        parts: list[Any] = []
        if msg.content:
            parts.append(msg.content)
        # Images/files are stored in metadata for Gemini-specific handling
        if msg.metadata.get("image"):
            parts.append(msg.metadata["image"])
        if msg.metadata.get("uploaded_file"):
            parts.append(msg.metadata["uploaded_file"])
        return parts if parts else [""]

    @staticmethod
    def _extract_usage(response) -> dict:
        meta = getattr(response, "usage_metadata", None)
        if meta:
            return {
                "prompt_tokens": getattr(meta, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(meta, "candidates_token_count", 0) or 0,
                "total_tokens": getattr(meta, "total_token_count", 0) or 0,
                "cached_tokens": getattr(meta, "cached_content_token_count", 0) or 0,
                "thinking_tokens": getattr(meta, "thoughts_token_count", 0) or 0,
            }
        return {}

    @staticmethod
    def _extract_grounding_metadata(response) -> list[dict[str, str]]:
        sources = []
        try:
            candidates = getattr(response, "candidates", [])
            if not candidates:
                return sources
            for candidate in candidates:
                grounding = getattr(candidate, "grounding_metadata", None)
                if not grounding:
                    continue
                chunks = getattr(grounding, "grounding_chunks", []) or []
                for chunk in chunks:
                    web = getattr(chunk, "web", None)
                    if web:
                        title = getattr(web, "title", "") or ""
                        uri = getattr(web, "uri", "") or ""
                        if uri:
                            sources.append({"title": title, "uri": uri})
        except Exception as e:
            logger.debug(f"Failed to extract grounding metadata: {e}")
        return sources
