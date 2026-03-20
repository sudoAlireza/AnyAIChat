import json
import os
import logging
import time
from typing import List, Dict, Any, Optional, Tuple

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

from config import GEMINI_MODEL, MAX_HISTORY_MESSAGES, SAFETY_OVERRIDE
from monitoring.metrics import metrics

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


class GeminiChat:
    def __init__(self, gemini_token: str, chat_history: List[Dict[str, Any]] = None, model_name: str = None, tools: List[str] = None, system_instruction: str = None, knowledge_base: List[Dict[str, Any]] = None, pinned_context: str = None, language: str = None):
        self.chat_history = chat_history if chat_history else []
        self.gemini_token = gemini_token
        self.system_instruction = system_instruction
        self.knowledge_base = knowledge_base if knowledge_base else []
        self.pinned_context = pinned_context
        self.language = language
        self.chat = None

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

    def _build_system_instruction(self) -> str:
        """Build system instruction string from user settings."""
        lang = self.language if self.language and self.language != "auto" else os.getenv("LANGUAGE", "en")
        lang_instruction = f"Please respond in {lang} language. " if lang != "auto" else "Respond in the same language the user writes in. "

        instruction = (
            f"{lang_instruction}"
            "Use standard Markdown formatting only: *bold*, _italic_, `code`, and ```code blocks```. "
            "Do NOT use headers (#), horizontal rules (---), or complex tables. "
            "Do NOT escape special characters with backslashes. Just write naturally."
        )

        if self.system_instruction:
            instruction += f"\n\nUser-defined persona instructions: {self.system_instruction}"

        if self.pinned_context:
            instruction += f"\n\nIMPORTANT persistent context from the user (always keep in mind): {self.pinned_context}"

        if self.knowledge_base:
            instruction += "\n\nYou have access to the following documents from your knowledge base (context preview):"
            for doc in self.knowledge_base:
                instruction += f"\n- {doc['file_name']}: {doc['content_preview']}"
            instruction += "\nUse this information when relevant to answer user queries."

        return instruction

    def _build_config(self, system_instruction: str = None) -> types.GenerateContentConfig:
        """Build GenerateContentConfig with tools, safety, and system instruction."""
        tool_list = []
        if "google_search" in self.tools:
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))

        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            safety_settings=self.safety_settings,
            tools=tool_list if tool_list else None,
        )

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
        """Extract token usage from response.usage_metadata."""
        meta = getattr(response, 'usage_metadata', None)
        if meta:
            return {
                "prompt_tokens": getattr(meta, 'prompt_token_count', 0) or 0,
                "completion_tokens": getattr(meta, 'candidates_token_count', 0) or 0,
                "total_tokens": getattr(meta, 'total_token_count', 0) or 0,
            }
        return None

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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    async def start_chat(self, image=None, file_path=None, file_mime_type=None) -> None:
        """Initialize a chat session. No API call for text-only chats."""
        sys_instruction = self._build_system_instruction()
        config = self._build_config(system_instruction=sys_instruction)

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
    async def send_message(self, message_text: str, image=None, file_path=None, file_mime_type=None) -> Tuple[str, Optional[Dict]]:
        """Send a message and return (response_text, usage_dict)."""
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
                return "No content to send.", None

            response = await self.chat.send_message(content)
            metrics.increment("gemini_messages_sent")
            usage = self._extract_usage(response)
            return response.text, usage
        except (ResourceExhausted, ServiceUnavailable):
            raise
        except Exception as e:
            logging.error(f"Failed to send message: {e}")
            metrics.increment("gemini_errors")
            return "Couldn't reach out to Google Gemini. Try Again...", None

    async def send_message_streaming(self, message_text: str, on_update, image=None, file_path=None, file_mime_type=None) -> Tuple[str, Optional[Dict]]:
        """Send message with native async streaming. Returns (final_text, usage_dict)."""
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
            return "No content to send.", None

        start_time = time.monotonic()
        full_text = ""
        last_update_time = 0
        usage = None

        async for chunk in await self.chat.send_message_stream(content):
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

        metrics.increment("gemini_messages_sent")
        metrics.record_latency("gemini_send_message_streaming", time.monotonic() - start_time)
        return full_text, usage

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

    async def get_chat_title(self) -> str:
        """Generate a title using a separate generate_content call (no history pollution)."""
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
            prompt = f"Write a one-line short title up to 10 words for a conversation about:\n{context}\n\nReturn only the title in plain text."
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
            )
            return response.text.strip()
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
    async def generate_plan(self, prompt: str, num_days: int = 30) -> str:
        """Fetch a structured N-day plan with an AI-generated title from Gemini."""
        milestone_days = [num_days // 4, num_days // 2, 3 * num_days // 4, num_days]
        milestones_str = ", ".join(str(d) for d in milestone_days)
        system_instruction = (
            "You are an expert curriculum designer and learning coach. "
            f"Create a detailed, progressive {num_days}-day learning/action plan for the topic below.\n\n"
            "Guidelines:\n"
            "- Structure the plan with clear phases (e.g., Week 1: Foundations, Week 2: Core Skills, etc.)\n"
            "- Each day should build on previous days — start simple, increase complexity gradually\n"
            "- Include a mix of theory, hands-on practice, and review/reflection days\n"
            "- Make titles concise but descriptive (3-6 words)\n"
            "- Make subjects actionable — describe what the user will DO that day, not just a topic name\n"
            f"- Add milestone/review days at day {milestones_str}\n\n"
            "Return ONLY a JSON object with these fields:\n"
            "1. 'title': a short CamelCase name for the entire plan (2-4 words, no spaces, "
            "e.g. 'PythonMachineLearning', 'GuitarForBeginners', 'DigitalMarketing'). "
            "This will be used as a hashtag identifier, so keep it concise and descriptive.\n"
            "2. 'plan': a JSON list of day objects, each with:\n"
            f"   - 'day': integer 1-{num_days}\n"
            "   - 'title': short title (3-6 words)\n"
            "   - 'subject': one actionable sentence describing the day's activity\n"
            "   - 'phase': which week/phase this belongs to (e.g., 'Week 1: Foundations')\n\n"
            "Do NOT include any markdown formatting like ```json. Return raw JSON only.\n\n"
            "Topic: "
        )
        try:
            config = self._build_config()
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=system_instruction + prompt,
                config=config,
            )
            return response.text.strip()
        except (ResourceExhausted, ServiceUnavailable):
            raise
        except Exception as e:
            logger.error(f"Failed to generate plan: {e}")
            return '{"title": "Plan", "plan": []}'

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
        """Use AI to parse a voice transcript into a command/action."""
        instruction = (
            "Analyze the following transcript and determine if the user wants to perform an action. "
            "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
            "Return the result as a JSON object with 'action' and 'parameters' (dict). "
            "Example for reminder: {'action': 'set_reminder', 'parameters': {'text': 'buy milk', 'time': 'tomorrow 5pm'}} "
            "Example for task: {'action': 'start_task', 'parameters': {'topic': 'learning python'}} "
            "Example for image: {'action': 'generate_image', 'parameters': {'prompt': 'a cat in space'}} "
            "Transcript: "
        )
        try:
            config = self._build_config()
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=instruction + transcript,
                config=config,
            )
            json_str = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to parse voice command: {e}")
            return {"action": "none", "parameters": {}}

    async def parse_voice_command_from_file(self, file_path: str) -> Dict[str, Any]:
        """Transcribe audio and parse command in a single API call."""
        instruction = (
            "Listen to this audio and determine if the user wants to perform an action. "
            "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
            "Return the result as a JSON object with 'action' and 'parameters' (dict). "
            "Example for reminder: {'action': 'set_reminder', 'parameters': {'text': 'buy milk', 'time': 'tomorrow 5pm'}} "
            "Example for task: {'action': 'start_task', 'parameters': {'topic': 'learning python'}} "
            "Example for image: {'action': 'generate_image', 'parameters': {'prompt': 'a cat in space'}} "
            "If it's just a normal message or question, return {'action': 'none', 'parameters': {}}."
        )
        try:
            uploaded_file = await self.client.aio.files.upload(
                file=file_path, config=types.UploadFileConfig(mime_type="audio/ogg")
            )
            config = self._build_config()
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=[instruction, uploaded_file],
                config=config,
            )
            json_str = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(json_str)
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
