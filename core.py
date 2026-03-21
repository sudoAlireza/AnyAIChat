import json
import os
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

from config import (
    GEMINI_MODEL, MAX_HISTORY_MESSAGES, SAFETY_OVERRIDE,
    CACHE_TTL_SECONDS, CACHE_MIN_TOKENS, RAG_TOP_K,
)
from schemas import PLAN_SCHEMA, CHAT_TITLE_SCHEMA, VOICE_COMMAND_SCHEMA
from monitoring.metrics import metrics

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


class GeminiChat:
    def __init__(self, gemini_token: str, chat_history: List[Dict[str, Any]] = None,
                 model_name: str = None, tools: List[str] = None,
                 system_instruction: str = None,
                 knowledge_base: List[Dict[str, Any]] = None,
                 pinned_context: str = None, language: str = None,
                 pool=None, user_id: int = None,
                 thinking_mode: str = "off", code_execution: bool = False):
        self.chat_history = chat_history if chat_history else []
        self.gemini_token = gemini_token
        self.system_instruction = system_instruction
        self.knowledge_base = knowledge_base if knowledge_base else []
        self.pinned_context = pinned_context
        self.language = language
        self.chat = None
        self.pool = pool
        self.user_id = user_id
        self.thinking_mode = thinking_mode
        self.code_execution = code_execution
        self._cached_content = None

        self.client = genai.Client(api_key=self.gemini_token)

        # Load safety settings with override support
        with open("./safety_settings.json", "r") as fp:
            raw_settings = json.load(fp)

        if SAFETY_OVERRIDE == "BLOCK_NONE":
            for setting in raw_settings:
                setting["threshold"] = "BLOCK_NONE"

        self.safety_settings = [
            types.SafetySetting(category=s["category"], threshold=s["threshold"])
            for s in raw_settings
        ]

        self.model_name = model_name if model_name else GEMINI_MODEL
        self.tools = tools if tools else []
        logging.info(f"Initiated new chat model: {self.model_name} with tools: {self.tools}")

    def _build_system_instruction(self, rag_context: str = None) -> str:
        """Build system instruction string from user settings."""
        lang = self.language if self.language and self.language != "auto" else os.getenv("LANGUAGE", "en")
        lang_instruction = f"Please respond in {lang} language. " if lang != "auto" else "Respond in the same language the user writes in. "

        instruction = (
            f"{lang_instruction}"
            "Format text using only bold (wrap with single asterisks), italic (wrap with underscores), inline code (wrap with backticks), and code blocks (wrap with triple backticks). "
            "Do NOT use headers, horizontal rules, or complex tables. "
            "Do NOT escape special characters with backslashes. Do NOT demonstrate or showcase formatting at the end of your response. Just write naturally."
        )

        if self.system_instruction:
            instruction += f"\n\nUser-defined persona instructions: {self.system_instruction}"

        if self.pinned_context:
            instruction += f"\n\nIMPORTANT persistent context from the user (always keep in mind): {self.pinned_context}"

        # Phase 5: Use RAG chunks if available, otherwise fall back to previews
        if rag_context:
            instruction += f"\n\nRelevant knowledge base context:\n{rag_context}"
            instruction += "\nUse this information when relevant to answer user queries."
        elif self.knowledge_base:
            instruction += "\n\nYou have access to the following documents from your knowledge base (context preview):"
            for doc in self.knowledge_base:
                instruction += f"\n- {doc['file_name']}: {doc['content_preview']}"
            instruction += "\nUse this information when relevant to answer user queries."

        return instruction

    def _build_config(self, system_instruction: str = None,
                      response_schema: dict = None,
                      cached_content: str = None) -> types.GenerateContentConfig:
        """Build GenerateContentConfig with tools, safety, and system instruction."""
        tool_list = []
        if "google_search" in self.tools:
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))

        # Phase 4: Code execution tool
        if self.code_execution:
            tool_list.append(types.Tool(code_execution=types.ToolCodeExecution()))

        kwargs = {
            "system_instruction": system_instruction,
            "safety_settings": self.safety_settings,
            "tools": tool_list if tool_list else None,
        }

        # Phase 1: Structured output
        if response_schema:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema

        # Phase 2: Context caching
        if cached_content:
            kwargs["cached_content"] = cached_content

        # Phase 3: Thinking mode
        if self.thinking_mode and self.thinking_mode != "off":
            budgets = {"light": 1024, "medium": 4096, "deep": 8192}
            budget = budgets.get(self.thinking_mode, 0)
            if budget > 0:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)

        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _convert_history(history_dicts: List[Dict]) -> List[types.Content]:
        """Convert stored dict history to types.Content objects."""
        result = []
        for entry in history_dicts:
            parts = []
            for p in entry.get("parts", []):
                if p.get("text"):
                    parts.append(types.Part.from_text(text=p["text"]))
            if parts:
                result.append(types.Content(role=entry["role"], parts=parts))
        return result

    @staticmethod
    def _extract_usage(response) -> Optional[Dict[str, int]]:
        """Extract token usage from response.usage_metadata, including cached and thinking tokens."""
        meta = getattr(response, 'usage_metadata', None)
        if meta:
            return {
                "prompt_tokens": getattr(meta, 'prompt_token_count', 0) or 0,
                "completion_tokens": getattr(meta, 'candidates_token_count', 0) or 0,
                "total_tokens": getattr(meta, 'total_token_count', 0) or 0,
                "cached_tokens": getattr(meta, 'cached_content_token_count', 0) or 0,
                "thinking_tokens": getattr(meta, 'thoughts_token_count', 0) or 0,
            }
        return None

    @staticmethod
    def _extract_grounding_metadata(response) -> List[Dict[str, str]]:
        """Extract grounding sources from response metadata (Phase 6)."""
        sources = []
        try:
            candidates = getattr(response, 'candidates', [])
            if not candidates:
                return sources
            for candidate in candidates:
                grounding = getattr(candidate, 'grounding_metadata', None)
                if not grounding:
                    continue
                chunks = getattr(grounding, 'grounding_chunks', []) or []
                for chunk in chunks:
                    web = getattr(chunk, 'web', None)
                    if web:
                        title = getattr(web, 'title', '') or ''
                        uri = getattr(web, 'uri', '') or ''
                        if uri:
                            sources.append({"title": title, "uri": uri})
        except Exception as e:
            logger.debug(f"Failed to extract grounding metadata: {e}")
        return sources

    @staticmethod
    async def list_models(api_key: str = None) -> List[Dict[str, Any]]:
        """List all models supported by the API."""
        try:
            client = genai.Client(api_key=api_key)
            models = []
            async for m in await client.aio.models.list():
                models.append({
                    'name': m.name,
                    'display_name': getattr(m, 'display_name', m.name),
                    'description': getattr(m, 'description', ''),
                })
            return models
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    # --- Phase 2: Context Caching ---

    async def create_or_get_cache(self) -> Optional[str]:
        """Create or retrieve a context cache for the current user.

        Returns the cache name if successful, None otherwise.
        """
        if not self.pool or not self.user_id:
            return None

        try:
            from database.database import get_active_cache, save_cache_record, get_user_knowledge_full

            # Check for existing active cache
            existing = await get_active_cache(self.pool, self.user_id, self.model_name)
            if existing:
                # Verify cache still exists in the API
                try:
                    cache = await self.client.aio.caches.get(name=existing["cache_name"])
                    if cache:
                        logger.info(f"Reusing existing cache: {existing['cache_name']}")
                        return existing["cache_name"]
                except Exception:
                    logger.info("Cached content expired in API, will recreate")
                    from database.database import delete_cache_record
                    await delete_cache_record(self.pool, existing["cache_name"])

            # Build cache content from system instruction + knowledge base
            sys_instruction = self._build_system_instruction()
            knowledge_docs = await get_user_knowledge_full(self.pool, self.user_id)

            cache_parts = [sys_instruction]
            for doc in knowledge_docs:
                if doc.get("full_content"):
                    cache_parts.append(f"\n\n--- Document: {doc['file_name']} ---\n{doc['full_content']}")

            full_content = "\n".join(cache_parts)
            # Rough token estimate: ~4 chars per token
            estimated_tokens = len(full_content) // 4

            if estimated_tokens < CACHE_MIN_TOKENS:
                logger.info(f"Content too small for caching ({estimated_tokens} est. tokens < {CACHE_MIN_TOKENS})")
                return None

            # Create cache via API
            cache = await self.client.aio.caches.create(
                model=self.model_name,
                config=types.CreateCachedContentConfig(
                    contents=[types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=full_content)],
                    )],
                    ttl=f"{CACHE_TTL_SECONDS}s",
                    display_name=f"user_{self.user_id}_{int(time.time())}",
                ),
            )

            cache_name = cache.name
            token_count = getattr(getattr(cache, 'usage_metadata', None), 'total_token_count', estimated_tokens)
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=CACHE_TTL_SECONDS)).isoformat()

            await save_cache_record(self.pool, self.user_id, cache_name, self.model_name, token_count, expires_at)
            logger.info(f"Created new cache: {cache_name} ({token_count} tokens)")
            return cache_name

        except Exception as e:
            logger.warning(f"Failed to create/get cache: {e}")
            return None

    async def _get_rag_context(self, query: str) -> Optional[str]:
        """Phase 5: Retrieve relevant knowledge chunks for a query using RAG."""
        if not self.pool or not self.user_id:
            return None

        try:
            from database.database import get_user_chunks_with_embeddings
            from helpers.embeddings import find_relevant_chunks

            chunks = await get_user_chunks_with_embeddings(self.pool, self.user_id)
            if not chunks:
                return None

            relevant = await find_relevant_chunks(self.client, query, chunks, top_k=RAG_TOP_K)
            if not relevant:
                return None

            context_parts = []
            for chunk in relevant:
                context_parts.append(chunk["chunk_text"])
            return "\n\n---\n\n".join(context_parts)

        except Exception as e:
            logger.debug(f"RAG retrieval failed, falling back to previews: {e}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    async def start_chat(self, image=None, file_path=None, file_mime_type=None) -> None:
        """Initialize a chat session. No API call for text-only chats."""
        sys_instruction = self._build_system_instruction()

        # Phase 2: Try to use cached content
        cached_content = await self.create_or_get_cache()
        config = self._build_config(
            system_instruction=sys_instruction,
            cached_content=cached_content,
        )
        self._cached_content = cached_content

        # Convert stored dict history to types.Content
        history = self._convert_history(self.chat_history)
        if len(history) > MAX_HISTORY_MESSAGES * 2:
            history = history[-(MAX_HISTORY_MESSAGES * 2):]
            logger.info(f"Truncated chat history to {len(history)} entries")

        self.chat = self.client.aio.chats.create(
            model=self.model_name,
            config=config,
            history=history if history else None,
        )

        if image:
            await self.chat.send_message(["Describe this image", image])

        if file_path and file_mime_type:
            try:
                uploaded_file = await self.client.aio.files.upload(
                    file=file_path, config=types.UploadFileConfig(mime_type=file_mime_type)
                )
                await self.chat.send_message(["Please summarize or explain this document.", uploaded_file])
            except Exception as e:
                logging.error(f"Failed to upload file: {e}")

        logging.info("Start new conversation")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    async def send_message(self, message_text: str, image=None, file_path=None, file_mime_type=None) -> Tuple[str, Optional[Dict], List[Dict]]:
        """Send a message and return (response_text, usage_dict, grounding_sources)."""
        try:
            content = []
            if message_text:
                content.append(message_text)
            if image:
                content.append(image)
            if file_path and file_mime_type:
                try:
                    uploaded_file = await self.client.aio.files.upload(
                        file=file_path, config=types.UploadFileConfig(mime_type=file_mime_type)
                    )
                    content.append(uploaded_file)
                except Exception as e:
                    logging.error(f"Failed to upload file in send_message: {e}")

            if not content:
                return "No content to send.", None, []

            response = await self.chat.send_message(content)
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            sources = self._extract_grounding_metadata(response)
            return response.text, usage, sources
        except (ResourceExhausted, ServiceUnavailable):
            raise
        except Exception as e:
            logging.error(f"Failed to send message: {e}")
            metrics.increment("gemini_errors")
            return "Couldn't reach out to Google Gemini. Try Again...", None, []

    async def send_message_streaming(self, message_text: str, on_update, image=None, file_path=None, file_mime_type=None) -> Tuple[str, Optional[Dict], List[Dict]]:
        """Send message with native async streaming. Returns (final_text, usage_dict, grounding_sources)."""
        content = []
        if message_text:
            content.append(message_text)
        if image:
            content.append(image)
        if file_path and file_mime_type:
            try:
                uploaded_file = await self.client.aio.files.upload(
                    file=file_path, config=types.UploadFileConfig(mime_type=file_mime_type)
                )
                content.append(uploaded_file)
            except Exception as e:
                logging.error(f"Failed to upload file in streaming: {e}")

        if not content:
            return "No content to send.", None, []

        start_time = time.monotonic()
        full_text = ""
        last_update_time = 0
        usage = None
        last_response = None

        async for chunk in await self.chat.send_message_stream(content):
            last_response = chunk
            if hasattr(chunk, 'text') and chunk.text:
                full_text += chunk.text
                now = time.monotonic()
                if now - last_update_time >= 1.5:
                    try:
                        await on_update(full_text)
                    except Exception:
                        pass
                    last_update_time = now
            chunk_usage = self._extract_usage(chunk)
            if chunk_usage:
                usage = chunk_usage

        # Phase 6: Extract grounding from the last chunk
        sources = self._extract_grounding_metadata(last_response) if last_response else []

        metrics.increment("gemini_messages_sent")
        metrics.record_latency("gemini_send_message_streaming", time.monotonic() - start_time)
        return full_text, usage, sources

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    async def one_shot(self, prompt: str) -> Tuple[str, Optional[Dict]]:
        """Single prompt->response with no chat session. Returns (text, usage_dict)."""
        try:
            sys_instruction = self._build_system_instruction()
            config = self._build_config(system_instruction=sys_instruction)
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            return response.text, usage
        except (ResourceExhausted, ServiceUnavailable):
            raise
        except Exception as e:
            logging.error(f"Failed one_shot: {e}")
            metrics.increment("gemini_errors")
            return "Couldn't reach out to Google Gemini. Try Again...", None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    async def one_shot_structured(self, prompt: str, schema: dict) -> Tuple[Optional[Dict], Optional[Dict]]:
        """Single prompt->structured JSON response. Returns (parsed_dict, usage_dict)."""
        try:
            sys_instruction = self._build_system_instruction()
            config = self._build_config(
                system_instruction=sys_instruction,
                response_schema=schema,
            )
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            parsed = json.loads(response.text)
            return parsed, usage
        except (ResourceExhausted, ServiceUnavailable):
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse structured response: {e}")
            return None, None
        except Exception as e:
            logging.error(f"Failed one_shot_structured: {e}")
            metrics.increment("gemini_errors")
            return None, None

    async def get_chat_title(self) -> str:
        """Generate a title using structured output (no history pollution)."""
        try:
            snippets = []
            history = self.chat.get_history() if hasattr(self.chat, 'get_history') else getattr(self.chat, '_curated_history', [])
            for msg in history:
                if msg.role == "user":
                    for part in msg.parts:
                        if hasattr(part, 'text') and part.text:
                            snippets.append(part.text[:100])
                            break
                if len(snippets) >= 3:
                    break
            context = "\n".join(snippets) if snippets else "General conversation"
            prompt = f"Write a one-line short title up to 10 words for a conversation about:\n{context}"
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CHAT_TITLE_SCHEMA,
            )
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
            parsed = json.loads(response.text)
            return parsed.get("title", "New Conversation").strip()
        except Exception as e:
            logger.warning(f"Failed to get chat title: {e}")
            return "New Conversation"

    def get_chat_history(self) -> List[Dict]:
        """Convert chat history to serializable format (list of dicts)."""
        serializable_history = []
        history = self.chat.get_history() if hasattr(self.chat, 'get_history') else getattr(self.chat, '_curated_history', [])
        for message in history:
            role = message.role
            parts = []
            for part in message.parts:
                if hasattr(part, 'text') and part.text:
                    parts.append({'text': part.text})
            serializable_history.append({'role': role, 'parts': parts})
        return serializable_history

    def get_history_length(self) -> int:
        """Return current chat history length."""
        if self.chat:
            history = self.chat.get_history() if hasattr(self.chat, 'get_history') else getattr(self.chat, '_curated_history', [])
            return len(history)
        return 0

    @staticmethod
    async def list_uploaded_files(api_key: str = None) -> List[Dict[str, Any]]:
        """List all files currently uploaded to Gemini API."""
        try:
            client = genai.Client(api_key=api_key)
            files = []
            async for f in await client.aio.files.list():
                files.append({
                    'name': f.name,
                    'display_name': getattr(f, 'display_name', ''),
                    'uri': getattr(f, 'uri', ''),
                    'create_time': str(getattr(f, 'create_time', '')),
                    'mime_type': getattr(f, 'mime_type', ''),
                    'size_bytes': getattr(f, 'size_bytes', 0),
                })
            return files
        except Exception as e:
            logger.error(f"Failed to list uploaded files: {e}")
            return []

    def close(self) -> None:
        logging.info("Closed model instance")
        self.chat = None
        self.chat_history = []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    async def generate_plan(self, prompt: str, num_days: int = 30) -> Dict[str, Any]:
        """Fetch a structured N-day plan with an AI-generated title from Gemini.

        Returns parsed dict with 'title' and 'plan' keys.
        """
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
            config = self._build_config(response_schema=PLAN_SCHEMA)
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=instruction + prompt,
                config=config,
            )
            return json.loads(response.text)
        except (ResourceExhausted, ServiceUnavailable):
            raise
        except Exception as e:
            logger.error(f"Failed to generate plan: {e}")
            return {"title": "Plan", "plan": []}

    async def generate_image(self, prompt: str):
        """Generate an image using Imagen model."""
        try:
            response = await self.client.aio.models.generate_images(
                model='imagen-3.0-generate-001',
                prompt=prompt,
                config=types.GenerateImagesConfig(number_of_images=1),
            )
            return response
        except Exception as e:
            logger.error(f"Failed to generate image: {e}")
            raise

    async def parse_voice_command(self, transcript: str) -> Dict[str, Any]:
        """Use AI to parse a voice transcript into a command/action with structured output."""
        instruction = (
            "Analyze the following transcript and determine if the user wants to perform an action. "
            "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
            "Transcript: "
        )
        try:
            config = self._build_config(response_schema=VOICE_COMMAND_SCHEMA)
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=instruction + transcript,
                config=config,
            )
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Failed to parse voice command: {e}")
            return {"action": "none", "parameters": {}}

    async def parse_voice_command_from_file(self, file_path: str) -> Dict[str, Any]:
        """Transcribe audio and parse command in a single API call with structured output."""
        instruction = (
            "Listen to this audio and determine if the user wants to perform an action. "
            "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
            "If it's just a normal message or question, use action 'none'."
        )
        try:
            uploaded_file = await self.client.aio.files.upload(
                file=file_path, config=types.UploadFileConfig(mime_type="audio/ogg")
            )
            config = self._build_config(response_schema=VOICE_COMMAND_SCHEMA)
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=[instruction, uploaded_file],
                config=config,
            )
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Failed to parse voice command from file: {e}")
            return {"action": "none", "parameters": {}}

    async def upload_file(self, file_path: str, mime_type: str):
        """Upload a file to the Gemini API."""
        return await self.client.aio.files.upload(
            file=file_path, config=types.UploadFileConfig(mime_type=mime_type)
        )

    async def generate_content_with_file(self, prompt: str, uploaded_file) -> str:
        """Generate content with an uploaded file. Returns response text."""
        config = self._build_config()
        response = await self.client.aio.models.generate_content(
            model=self.model_name,
            contents=[prompt, uploaded_file],
            config=config,
        )
        return response.text.strip()
