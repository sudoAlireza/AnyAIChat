import json
import os
import asyncio
import threading
import logging
import time
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

from config import GEMINI_MODEL, GEMINI_MAX_WORKERS, MAX_HISTORY_MESSAGES, SAFETY_OVERRIDE
from monitoring.metrics import metrics

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Thread pool for running synchronous SDK calls
_executor = ThreadPoolExecutor(max_workers=GEMINI_MAX_WORKERS)

# Lock for genai.configure() global state
_genai_lock = threading.Lock()


class GeminiChat:
    def __init__(self, gemini_token: str, chat_history: List[Dict[str, Any]] = None, model_name: str = None, tools: List[str] = None, system_instruction: str = None, knowledge_base: List[Dict[str, Any]] = None, pinned_context: str = None, language: str = None):
        self.chat_history = chat_history if chat_history else []
        self.gemini_token = gemini_token
        self.system_instruction = system_instruction
        self.knowledge_base = knowledge_base if knowledge_base else []
        self.pinned_context = pinned_context
        self.language = language

        with _genai_lock:
            genai.configure(api_key=self.gemini_token)

        # Load safety settings with override support
        with open("./safety_settings.json", "r") as fp:
            self.safety_settings = json.load(fp)

        if SAFETY_OVERRIDE == "BLOCK_NONE":
            for setting in self.safety_settings:
                setting["threshold"] = "BLOCK_NONE"

        self.model_name = model_name if model_name else GEMINI_MODEL
        self.tools = tools if tools else []
        logging.info(f"Initiated new chat model: {self.model_name} with tools: {self.tools}")

    def _build_system_instruction(self):
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

    def _get_model(self, system_instruction: str = None):
        try:
            with _genai_lock:
                genai.configure(api_key=self.gemini_token)
                model_tools = []
                if "google_search" in self.tools:
                    model_tools.append(genai.protos.Tool(
                        google_search_retrieval=genai.protos.GoogleSearchRetrieval()
                    ))

                return genai.GenerativeModel(
                    self.model_name,
                    safety_settings=self.safety_settings,
                    tools=model_tools if model_tools else None,
                    system_instruction=system_instruction,
                )
        except Exception as e:
            logging.error(f"Failed to get model: {e}")
            raise

    @staticmethod
    def list_models(api_key: str = None) -> List[Dict[str, Any]]:
        """List all models supported by the API that are available for generation."""
        try:
            with _genai_lock:
                if api_key:
                    genai.configure(api_key=api_key)
                models = []
                for m in genai.list_models():
                    if 'generateContent' in m.supported_generation_methods:
                        models.append({
                            'name': m.name,
                            'display_name': m.display_name,
                            'description': m.description
                        })
            return models
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    def start_chat(self, image=None, file_path=None, file_mime_type=None) -> None:
        # Build system instruction and pass it to the model (no API call, no tokens used)
        sys_instruction = self._build_system_instruction()
        model = self._get_model(system_instruction=sys_instruction)

        # Prepare initial history, truncated to MAX_HISTORY_MESSAGES pairs
        history = []
        if self.chat_history:
            if len(self.chat_history) > MAX_HISTORY_MESSAGES * 2:
                history = self.chat_history[-(MAX_HISTORY_MESSAGES * 2):]
                logger.info(f"Truncated chat history from {len(self.chat_history)} to {len(history)} entries")
            else:
                history.extend(self.chat_history)

        self.chat = model.start_chat(history=history)

        if image:
            prompt = "Describe this image"
            self.chat.send_message([prompt, image])

        if file_path and file_mime_type:
            try:
                with _genai_lock:
                    genai.configure(api_key=self.gemini_token)
                    uploaded_file = genai.upload_file(path=file_path, mime_type=file_mime_type)
                prompt = "Please summarize or explain this document."
                self.chat.send_message([prompt, uploaded_file])
            except Exception as e:
                logging.error(f"Failed to upload file: {e}")

        logging.info("Start new conversation")

    async def async_start_chat(self, image=None, file_path=None, file_mime_type=None) -> None:
        """Async wrapper for start_chat."""
        loop = asyncio.get_event_loop()
        start_time = time.monotonic()
        await loop.run_in_executor(_executor, lambda: self.start_chat(image, file_path, file_mime_type))
        metrics.record_latency("gemini_start_chat", time.monotonic() - start_time)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    def send_message(self, message_text: str, image=None, file_path=None, file_mime_type=None) -> str:
        try:
            content = []
            if message_text:
                content.append(message_text)
            if image:
                content.append(image)
            if file_path and file_mime_type:
                try:
                    with _genai_lock:
                        genai.configure(api_key=self.gemini_token)
                        uploaded_file = genai.upload_file(path=file_path, mime_type=file_mime_type)
                    content.append(uploaded_file)
                except Exception as e:
                    logging.error(f"Failed to upload file in send_message: {e}")

            if not content:
                return "No content to send."

            response = self.chat.send_message(content)

            grounding_metadata = getattr(response, 'grounding_metadata', None)
            result_text = response.text

            metrics.increment("gemini_messages_sent")
            return result_text
        except (ResourceExhausted, ServiceUnavailable):
            raise  # Let tenacity handle retries
        except Exception as e:
            logging.error(f"Failed to send message: {e}")
            metrics.increment("gemini_errors")
            return "Couldn't reach out to Google Gemini. Try Again..."

    async def async_send_message(self, message_text: str, image=None, file_path=None, file_mime_type=None) -> str:
        """Async wrapper for send_message."""
        loop = asyncio.get_event_loop()
        start_time = time.monotonic()
        result = await loop.run_in_executor(
            _executor, lambda: self.send_message(message_text, image, file_path, file_mime_type)
        )
        metrics.record_latency("gemini_send_message", time.monotonic() - start_time)
        return result

    async def async_send_message_streaming(self, message_text: str, on_update, image=None, file_path=None, file_mime_type=None) -> str:
        """Send message with streaming, calling on_update periodically with accumulated text."""
        loop = asyncio.get_event_loop()
        result_queue = asyncio.Queue()

        def _stream():
            try:
                content = []
                if message_text:
                    content.append(message_text)
                if image:
                    content.append(image)
                if file_path and file_mime_type:
                    try:
                        with _genai_lock:
                            genai.configure(api_key=self.gemini_token)
                            uploaded_file = genai.upload_file(path=file_path, mime_type=file_mime_type)
                        content.append(uploaded_file)
                    except Exception as e:
                        logging.error(f"Failed to upload file in streaming: {e}")

                if not content:
                    loop.call_soon_threadsafe(result_queue.put_nowait, ('done', 'No content to send.'))
                    return

                response = self.chat.send_message(content, stream=True)
                full_text = ""
                for chunk in response:
                    if hasattr(chunk, 'text') and chunk.text:
                        full_text += chunk.text
                        loop.call_soon_threadsafe(result_queue.put_nowait, ('chunk', full_text))

                metrics.increment("gemini_messages_sent")
                loop.call_soon_threadsafe(result_queue.put_nowait, ('done', full_text))
            except Exception as e:
                loop.call_soon_threadsafe(result_queue.put_nowait, ('error', e))

        start_time = time.monotonic()
        _executor.submit(_stream)

        last_update_time = 0
        final_text = ""
        while True:
            try:
                msg_type, data = await asyncio.wait_for(result_queue.get(), timeout=60)
                if msg_type == 'done':
                    final_text = data
                    break
                elif msg_type == 'error':
                    raise data
                elif msg_type == 'chunk':
                    now = time.monotonic()
                    if now - last_update_time >= 1.5:
                        try:
                            await on_update(data)
                        except Exception:
                            pass
                        last_update_time = now
                    final_text = data
            except asyncio.TimeoutError:
                if final_text:
                    break
                raise TimeoutError("Streaming response timed out")

        metrics.record_latency("gemini_send_message_streaming", time.monotonic() - start_time)
        return final_text

    def get_chat_title(self) -> str:
        try:
            response = self.chat.send_message("Write a one-line short title up to 10 words for this conversation in plain text.")
            return response.text.strip()
        except Exception as e:
            logger.warning(f"Failed to get chat title: {e}")
            return "New Conversation"

    async def async_get_chat_title(self) -> str:
        """Async wrapper for get_chat_title."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self.get_chat_title)

    def get_chat_history(self):
        """Convert history to a serializable format (list of dicts)."""
        serializable_history = []
        for message in self.chat.history:
            role = message.role
            parts = []
            for part in message.parts:
                if hasattr(part, 'text'):
                    parts.append({'text': part.text})
            serializable_history.append({'role': role, 'parts': parts})
        return serializable_history

    def get_history_length(self) -> int:
        """Return current chat history length."""
        if self.chat and hasattr(self.chat, 'history'):
            return len(self.chat.history)
        return 0

    @staticmethod
    def list_uploaded_files(api_key: str = None) -> List[Dict[str, Any]]:
        """List all files currently uploaded to Gemini API."""
        try:
            with _genai_lock:
                if api_key:
                    genai.configure(api_key=api_key)
                files = []
                for f in genai.list_files():
                    files.append({
                        'name': f.name,
                        'display_name': f.display_name,
                        'uri': f.uri,
                        'create_time': str(f.create_time),
                        'mime_type': f.mime_type,
                        'size_bytes': f.size_bytes
                    })
            return files
        except Exception as e:
            logger.error(f"Failed to list uploaded files: {e}")
            return []

    @staticmethod
    async def async_list_models(api_key: str = None) -> List[Dict[str, Any]]:
        """Async wrapper for list_models."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: GeminiChat.list_models(api_key))

    @staticmethod
    async def async_list_uploaded_files(api_key: str = None) -> List[Dict[str, Any]]:
        """Async wrapper for list_uploaded_files."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: GeminiChat.list_uploaded_files(api_key))

    def close(self) -> None:
        logging.info("Closed model instance")
        self.chat = None
        self.chat_history = []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10),
           retry=retry_if_exception_type((ResourceExhausted, ServiceUnavailable)))
    def generate_plan(self, prompt: str, num_days: int = 30) -> str:
        """Fetch a structured N-day plan with an AI-generated title from Gemini.

        Returns a JSON string of {"title": "CamelCaseTitle", "plan": [...]}.
        """
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
            model = self._get_model()
            response = model.generate_content(system_instruction + prompt)
            return response.text.strip()
        except (ResourceExhausted, ServiceUnavailable):
            raise  # Let tenacity handle retries
        except Exception as e:
            logger.error(f"Failed to generate plan: {e}")
            return '{"title": "Plan", "plan": []}'

    async def async_generate_plan(self, prompt: str, num_days: int = 30) -> str:
        """Async wrapper for generate_plan."""
        loop = asyncio.get_event_loop()
        start_time = time.monotonic()
        result = await loop.run_in_executor(_executor, lambda: self.generate_plan(prompt, num_days))
        metrics.record_latency("gemini_generate_plan", time.monotonic() - start_time)
        return result

    def generate_image(self, prompt: str) -> str:
        """Generate an image using Imagen model if supported by API."""
        try:
            with _genai_lock:
                genai.configure(api_key=self.gemini_token)
                model = genai.GenerativeModel("imagen-3.0-generate-001")
            response = model.generate_content(prompt)
            return response
        except Exception as e:
            logger.error(f"Failed to generate image: {e}")
            raise

    async def async_generate_image(self, prompt: str):
        """Async wrapper for generate_image."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: self.generate_image(prompt))

    def parse_voice_command(self, transcript: str) -> Dict[str, Any]:
        """Use AI to parse a voice transcript into a command/action."""
        system_instruction = (
            "Analyze the following transcript and determine if the user wants to perform an action. "
            "Actions include: 'start_task' (for 30-day plans), 'set_reminder', 'generate_image', or 'none'. "
            "Return the result as a JSON object with 'action' and 'parameters' (dict). "
            "Example for reminder: {'action': 'set_reminder', 'parameters': {'text': 'buy milk', 'time': 'tomorrow 5pm'}} "
            "Example for task: {'action': 'start_task', 'parameters': {'topic': 'learning python'}} "
            "Example for image: {'action': 'generate_image', 'parameters': {'prompt': 'a cat in space'}} "
            "Transcript: "
        )
        try:
            model = self._get_model()
            response = model.generate_content(system_instruction + transcript)
            json_str = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to parse voice command: {e}")
            return {"action": "none", "parameters": {}}

    async def async_parse_voice_command(self, transcript: str) -> Dict[str, Any]:
        """Async wrapper for parse_voice_command."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: self.parse_voice_command(transcript))
