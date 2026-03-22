"""Provider-agnostic chat session — replaces GeminiChat as the single point of interaction."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from providers.base import (
    AIProvider,
    ChatMessage,
    ChatResponse,
    ProviderError,
)
from providers.registry import ProviderRegistry
from chat.system_prompt import build_system_instruction

logger = logging.getLogger(__name__)


class ChatSession:
    """Provider-agnostic chat session managing history and delegating to a provider.

    Replaces the old GeminiChat class. Handlers interact with this class
    and never touch provider-specific code directly.
    """

    def __init__(
        self,
        provider_name: str,
        api_key: str,
        model_name: str,
        history: list[dict] | None = None,
        system_instruction: str | None = None,
        knowledge_base: list[dict] | None = None,
        pinned_context: str | None = None,
        language: str | None = None,
        pool: Any = None,
        user_id: int | None = None,
        thinking_mode: str = "off",
        code_execution: bool = False,
        web_search: bool = False,
    ) -> None:
        self.provider_name = provider_name
        self.api_key = api_key
        self.model_name = model_name
        self.system_instruction_raw = system_instruction
        self.knowledge_base = knowledge_base or []
        self.pinned_context = pinned_context
        self.language = language
        self.pool = pool
        self.user_id = user_id
        self.thinking_mode = thinking_mode
        self.code_execution = code_execution
        self.web_search = web_search

        # History is stored in universal ChatMessage format
        self._history: list[ChatMessage] = self._convert_history(history or [])

        # Resolved at start_chat time
        self._system_instruction: str = ""
        self._cached_content: str | None = None

    @property
    def provider(self) -> AIProvider:
        p = ProviderRegistry().get(self.provider_name)
        if not p:
            raise ProviderError(f"Provider '{self.provider_name}' not registered")
        return p

    # ----- History conversion -----

    @staticmethod
    def _convert_history(history_dicts: list[dict]) -> list[ChatMessage]:
        """Convert stored dict history to ChatMessage objects.

        Handles both old Gemini format {role, parts} and new universal format {role, content}.
        """
        result = []
        for entry in history_dicts:
            # New universal format
            if "content" in entry:
                role = entry["role"]
                if role == "model":
                    role = "assistant"
                result.append(ChatMessage(
                    role=role,
                    content=entry["content"],
                    metadata=entry.get("metadata", {}),
                ))
            # DEPRECATED: Old Gemini format (remove once all histories are migrated)
            elif "parts" in entry:
                role = entry["role"]
                if role == "model":
                    role = "assistant"
                text_parts = []
                for p in entry.get("parts", []):
                    if isinstance(p, dict) and p.get("text"):
                        text_parts.append(p["text"])
                    elif isinstance(p, str):
                        text_parts.append(p)
                if text_parts:
                    result.append(ChatMessage(
                        role=role,
                        content="\n".join(text_parts),
                        metadata={"provider": "gemini", "migrated": True},
                    ))
        return result

    def get_history_as_dicts(self) -> list[dict]:
        """Export history in the universal format for DB storage."""
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "metadata": msg.metadata,
            }
            for msg in self._history
        ]

    def get_history_length(self) -> int:
        return len(self._history)

    # ----- Session lifecycle -----

    async def start_chat(self, image: Any = None, file_path: str | None = None,
                         file_mime_type: str | None = None) -> None:
        """Initialize the chat session. Build system instruction, optionally cache content."""
        # Build RAG context if available
        rag_context = await self._get_rag_context("") if self.knowledge_base else None

        self._system_instruction = build_system_instruction(
            language=self.language,
            system_instruction=self.system_instruction_raw,
            pinned_context=self.pinned_context,
            knowledge_base=self.knowledge_base,
            rag_context=rag_context,
        )

        # Gemini-specific: try context caching
        if self.provider_name == "gemini" and self.pool and self.user_id:
            try:
                from database.database import get_user_knowledge_full
                knowledge_docs = await get_user_knowledge_full(self.pool, self.user_id)
                gemini = self.provider
                if hasattr(gemini, "create_or_get_cache"):
                    self._cached_content = await gemini.create_or_get_cache(
                        self.api_key, self.model_name, self.pool, self.user_id,
                        self._system_instruction, knowledge_docs,
                    )
            except Exception as e:
                logger.warning(f"Context caching failed: {e}")

        # Handle initial image/file upload for the first message
        if image or (file_path and file_mime_type):
            if self.provider_name == "gemini" and hasattr(self.provider, "upload_file"):
                if image:
                    self._history.append(ChatMessage(
                        role="user",
                        content="Describe this image",
                        metadata={"image": image},
                    ))
                if file_path and file_mime_type:
                    try:
                        uploaded = await self.provider.upload_file(self.api_key, file_path, file_mime_type)
                        self._history.append(ChatMessage(
                            role="user",
                            content="Please summarize or explain this document.",
                            metadata={"uploaded_file": uploaded},
                        ))
                    except Exception as e:
                        logger.error(f"Failed to upload file: {e}")

        logger.info(f"Started chat session: provider={self.provider_name}, model={self.model_name}")

    async def send_message(
        self,
        text: str,
        image: Any = None,
        file_path: str | None = None,
        file_mime_type: str | None = None,
    ) -> ChatResponse:
        """Send a message and return the response."""
        # Build RAG context for this query
        rag_context = await self._get_rag_context(text) if self.knowledge_base else None
        if rag_context:
            sys_instr = build_system_instruction(
                language=self.language,
                system_instruction=self.system_instruction_raw,
                pinned_context=self.pinned_context,
                rag_context=rag_context,
            )
        else:
            sys_instr = self._system_instruction

        # Build user message
        metadata: dict[str, Any] = {}
        if image:
            metadata["image"] = image
        if file_path and file_mime_type:
            if self.provider_name == "gemini" and hasattr(self.provider, "upload_file"):
                try:
                    uploaded = await self.provider.upload_file(self.api_key, file_path, file_mime_type)
                    metadata["uploaded_file"] = uploaded
                except Exception as e:
                    logger.error(f"Failed to upload file: {e}")

        user_msg = ChatMessage(role="user", content=text or "", metadata=metadata)
        self._history.append(user_msg)

        try:
            response = await self.provider.chat(
                api_key=self.api_key,
                model=self.model_name,
                messages=self._history,
                system_instruction=sys_instr,
                thinking_mode=self.thinking_mode,
                code_execution=self.code_execution,
                web_search=self.web_search,
                cached_content=self._cached_content,
            )
        except ProviderError:
            raise
        except Exception as e:
            logger.error(f"Chat error: {e}")
            raise

        # Add assistant response to history
        self._history.append(ChatMessage(
            role="assistant",
            content=response.text,
            metadata={"provider": self.provider_name},
        ))

        return response

    async def send_message_streaming(
        self,
        text: str,
        on_update: Callable[[str], Any],
        image: Any = None,
        file_path: str | None = None,
        file_mime_type: str | None = None,
    ) -> ChatResponse:
        """Send a message with streaming. Calls on_update with accumulated text."""
        rag_context = await self._get_rag_context(text) if self.knowledge_base else None
        if rag_context:
            sys_instr = build_system_instruction(
                language=self.language,
                system_instruction=self.system_instruction_raw,
                pinned_context=self.pinned_context,
                rag_context=rag_context,
            )
        else:
            sys_instr = self._system_instruction

        metadata: dict[str, Any] = {}
        if image:
            metadata["image"] = image
        if file_path and file_mime_type:
            if self.provider_name == "gemini" and hasattr(self.provider, "upload_file"):
                try:
                    uploaded = await self.provider.upload_file(self.api_key, file_path, file_mime_type)
                    metadata["uploaded_file"] = uploaded
                except Exception as e:
                    logger.error(f"Failed to upload file: {e}")

        user_msg = ChatMessage(role="user", content=text or "", metadata=metadata)
        self._history.append(user_msg)

        try:
            response = await self.provider.chat_stream(
                api_key=self.api_key,
                model=self.model_name,
                messages=self._history,
                system_instruction=sys_instr,
                on_update=on_update,
                thinking_mode=self.thinking_mode,
                code_execution=self.code_execution,
                web_search=self.web_search,
                cached_content=self._cached_content,
            )
        except ProviderError:
            raise
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            raise

        self._history.append(ChatMessage(
            role="assistant",
            content=response.text,
            metadata={"provider": self.provider_name},
        ))

        return response

    async def one_shot(self, prompt: str) -> ChatResponse:
        """Single prompt→response with no chat context."""
        if hasattr(self.provider, "one_shot"):
            return await self.provider.one_shot(
                api_key=self.api_key,
                model=self.model_name,
                prompt=prompt,
                system_instruction=self._system_instruction,
                thinking_mode=self.thinking_mode,
                code_execution=self.code_execution,
                web_search=self.web_search,
            )
        # Fallback: use regular chat with single message
        return await self.provider.chat(
            api_key=self.api_key,
            model=self.model_name,
            messages=[ChatMessage(role="user", content=prompt)],
            system_instruction=self._system_instruction,
        )

    async def one_shot_structured(self, prompt: str, schema: dict) -> tuple[dict | None, dict]:
        """Single prompt→structured JSON response. Returns (parsed_dict, usage)."""
        if hasattr(self.provider, "chat_structured"):
            resp = await self.provider.chat_structured(
                api_key=self.api_key,
                model=self.model_name,
                messages=[ChatMessage(role="user", content=prompt)],
                schema=schema,
                system_instruction=self._system_instruction,
            )
            parsed = resp.metadata.get("parsed")
            return parsed, resp.usage

        # Fallback: ask for JSON in the prompt
        json_prompt = (
            f"{prompt}\n\n"
            f"You MUST respond with ONLY valid JSON matching this schema (no markdown, no explanation, no code fences):\n"
            f"{json.dumps(schema)}"
        )
        resp = await self.one_shot(json_prompt)
        try:
            parsed = json.loads(resp.text)
            return parsed, resp.usage
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code fences or surrounding text
            text = resp.text
            import re as _re
            json_match = _re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
            if not json_match:
                json_match = _re.search(r'(\{[\s\S]*?\})', text)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                    return parsed, resp.usage
                except json.JSONDecodeError:
                    pass
            return None, resp.usage

    async def get_chat_title(self) -> str:
        """Generate a title for the current conversation."""
        from schemas import CHAT_TITLE_SCHEMA
        snippets = []
        for msg in self._history:
            if msg.role == "user" and msg.content:
                snippets.append(msg.content[:100])
                if len(snippets) >= 3:
                    break

        context = "\n".join(snippets) if snippets else "General conversation"
        prompt = f"Write a one-line short title up to 10 words for a conversation about:\n{context}"

        try:
            parsed, _ = await self.one_shot_structured(prompt, CHAT_TITLE_SCHEMA)
            if parsed:
                return parsed.get("title", "New Conversation").strip()
        except Exception as e:
            logger.warning(f"Failed to get chat title: {e}")
        return "New Conversation"

    async def generate_plan(self, prompt: str, num_days: int = 30) -> dict:
        """Generate a structured N-day learning plan."""
        from schemas import PLAN_SCHEMA
        milestone_days = [num_days // 4, num_days // 2, 3 * num_days // 4, num_days]
        milestones_str = ", ".join(str(d) for d in milestone_days)
        instruction = (
            "You are an expert curriculum designer and learning coach. "
            f"Create a detailed, progressive {num_days}-day learning/action plan for the topic below.\n\n"
            "Guidelines:\n"
            "- Structure the plan with clear phases (e.g., Week 1: Foundations, Week 2: Core Skills, etc.)\n"
            "- Each day should build on previous days — start simple, increase complexity gradually\n"
            "- Include a mix of theory, hands-on practice, and review/reflection days\n"
            "- Make titles concise but descriptive (3-6 words)\n"
            "- Make subjects actionable — describe what the user will DO that day, not just a topic name\n"
            f"- Add milestone/review days at day {milestones_str}\n\n"
            "Topic: "
        )
        try:
            parsed, _ = await self.one_shot_structured(instruction + prompt, PLAN_SCHEMA)
            if parsed and isinstance(parsed.get("plan"), list) and parsed["plan"]:
                return parsed
        except Exception as e:
            logger.warning(f"Plan generation failed with {self.provider_name}/{self.model_name}: {e}")

        # Fallback: use Gemini for structured plan generation if available and not already active
        if self.provider_name != "gemini":
            try:
                from providers.registry import ProviderRegistry
                gemini = ProviderRegistry().get("gemini")
                if gemini and self.api_key:
                    # Try with user's Gemini key if available
                    from database.database import get_user_api_key
                    gemini_key = None
                    if self.pool and self.user_id:
                        key_row = await get_user_api_key(self.pool, self.user_id, "gemini")
                        if key_row and key_row.get("api_key"):
                            gemini_key = key_row["api_key"]
                    if gemini_key:
                        logger.info("Falling back to Gemini for plan generation")
                        fallback = ChatSession(
                            provider_name="gemini",
                            api_key=gemini_key,
                            model_name="gemini-2.0-flash",
                        )
                        await fallback.start_chat()
                        parsed, _ = await fallback.one_shot_structured(instruction + prompt, PLAN_SCHEMA)
                        if parsed and isinstance(parsed.get("plan"), list) and parsed["plan"]:
                            return parsed
            except Exception as fb_err:
                logger.warning(f"Gemini fallback for plan also failed: {fb_err}")

        logger.error("All plan generation attempts failed")
        return {"title": "Plan", "plan": []}

    async def generate_image(self, prompt: str) -> Any:
        """Generate an image (delegates to provider if supported)."""
        if hasattr(self.provider, "generate_image"):
            return await self.provider.generate_image(self.api_key, prompt)
        raise ProviderError(f"Provider '{self.provider_name}' does not support image generation")

    async def parse_voice_command(self, transcript: str) -> dict:
        """Parse a voice transcript into a command/action."""
        from schemas import VOICE_COMMAND_SCHEMA
        instruction = (
            "Analyze the following transcript and determine if the user wants to perform an action. "
            "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
            "Transcript: "
        )
        try:
            parsed, _ = await self.one_shot_structured(instruction + transcript, VOICE_COMMAND_SCHEMA)
            return parsed or {"action": "none", "parameters": {}}
        except Exception:
            return {"action": "none", "parameters": {}}

    async def parse_voice_command_from_file(self, file_path: str) -> dict:
        """Transcribe audio and parse command (Gemini-specific, uses file upload)."""
        if self.provider_name == "gemini" and hasattr(self.provider, "upload_file"):
            instruction = (
                "Listen to this audio and determine if the user wants to perform an action. "
                "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
                "If it's just a normal message or question, use action 'none'."
            )
            try:
                uploaded = await self.provider.upload_file(self.api_key, file_path, "audio/ogg")
                # Use Gemini-specific content-with-file
                resp_text = await self.provider.generate_content_with_file(
                    self.api_key, self.model_name,
                    instruction + "\nRespond with JSON: {\"action\": \"...\", \"parameters\": {...}}",
                    uploaded,
                )
                return json.loads(resp_text)
            except Exception as e:
                logger.error(f"Failed to parse voice command from file: {e}")
        return {"action": "none", "parameters": {}}

    def close(self) -> None:
        """Clean up the session."""
        logger.info("Closed chat session")
        self._history.clear()

    # ----- Internal -----

    async def _get_rag_context(self, query: str) -> str | None:
        """Retrieve relevant knowledge chunks using RAG."""
        if not self.pool or not self.user_id:
            return None
        try:
            from database.database import get_user_chunks_with_embeddings
            from helpers.embeddings import find_relevant_chunks

            chunks = await get_user_chunks_with_embeddings(self.pool, self.user_id)
            if not chunks:
                return None

            # For RAG embedding, we need an embedding-capable provider
            if self.provider_name == "gemini":
                from google import genai as genai_module
                client = genai_module.Client(api_key=self.api_key)
                relevant = await find_relevant_chunks(client, query, chunks, top_k=5)
            else:
                # Non-Gemini: fall back to keyword matching or skip RAG
                return None

            if not relevant:
                return None

            return "\n\n---\n\n".join(chunk["chunk_text"] for chunk in relevant)
        except Exception as e:
            logger.debug(f"RAG retrieval failed: {e}")
            return None
