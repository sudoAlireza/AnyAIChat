import json
import os
import google.generativeai as genai
import logging
from typing import List, Dict, Any

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

class GeminiChat:
    def __init__(self, gemini_token: str, chat_history: List[Dict[str, Any]] = None, model_name: str = None, tools: List[str] = None, system_instruction: str = None, knowledge_base: List[Dict[str, Any]] = None):
        self.chat_history = chat_history if chat_history else []
        self.gemini_token = gemini_token
        self.system_instruction = system_instruction
        self.knowledge_base = knowledge_base if knowledge_base else []
        # Each instance configures the global genai with its own key
        genai.configure(api_key=self.gemini_token)
        with open("./safety_settings.json", "r") as fp:
            self.safety_settings = json.load(fp)
        self.model_name = model_name if model_name else os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        self.tools = tools if tools else []
        logging.info(f"Initiated new chat model: {self.model_name} with tools: {self.tools}")

    def _get_model(self):
        try:
            genai.configure(api_key=self.gemini_token)
            model_tools = []
            if "google_search" in self.tools:
                model_tools.append({"google_search": {}})
            
            return genai.GenerativeModel(
                self.model_name, 
                safety_settings=self.safety_settings,
                tools=model_tools if model_tools else None
            )
        except Exception as e:
            logging.error(f"Failed to get model: {e}")
            raise

    @staticmethod
    def list_models(api_key: str = None) -> List[Dict[str, Any]]:
        """List all models supported by the API that are available for generation."""
        try:
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

    def start_chat(self, image=None, file_path=None, file_mime_type=None) -> None:
        model = self._get_model()
        
        # Prepare initial history
        history = []
        if self.chat_history:
            history.extend(self.chat_history)
            
        lang = os.getenv("LANGUAGE", "en")
        self.chat = model.start_chat(history=history)
        
        # System instructions
        if not history:
            base_instruction = (
                f"You are a helpful assistant with a female persona. Please respond in {lang} language. "
                "Please use Telegram-compatible markdown (MarkdownV2). "
                "Use *bold* for bold text, _italic_ for italic, and `code` for code blocks. "
                "Do NOT use headers (#), horizontal rules (---), or complex tables. "
                "Always escape special characters if necessary, but keep it simple."
            )
            
            if self.system_instruction:
                base_instruction += f"\n\nAdditional instructions for your persona: {self.system_instruction}"
            
            if self.knowledge_base:
                base_instruction += "\n\nYou have access to the following documents from your knowledge base (context preview):"
                for doc in self.knowledge_base:
                    base_instruction += f"\n- {doc['file_name']}: {doc['content_preview']}"
                base_instruction += "\nUse this information when relevant to answer user queries."
                
            self.chat.send_message(base_instruction)
            
        if image:
            # If we have an image at the start, we send it as the first user message
            prompt = "Describe this image"
            self.chat.send_message([prompt, image])
        
        if file_path and file_mime_type:
            # Upload file and send it
            try:
                uploaded_file = genai.upload_file(path=file_path, mime_type=file_mime_type)
                prompt = "Please summarize or explain this document."
                self.chat.send_message([prompt, uploaded_file])
            except Exception as e:
                logging.error(f"Failed to upload file: {e}")
            
        logging.info("Start new conversation")

    def send_message(self, message_text: str, image=None, file_path=None, file_mime_type=None) -> str:
        try:
            content = []
            if message_text:
                content.append(message_text)
            if image:
                content.append(image)
            if file_path and file_mime_type:
                try:
                    uploaded_file = genai.upload_file(path=file_path, mime_type=file_mime_type)
                    content.append(uploaded_file)
                except Exception as e:
                    logging.error(f"Failed to upload file in send_message: {e}")
            
            if not content:
                return "No content to send."
                
            response = self.chat.send_message(content)
            
            # Grounding check (if available)
            grounding_metadata = getattr(response, 'grounding_metadata', None)
            result_text = response.text
            if grounding_metadata and hasattr(grounding_metadata, 'search_entry_point'):
                # Add grounding info if user wants (or just always for now)
                # result_text += "\n\n(Information retrieved using Google Search)"
                pass
                
            return result_text
        except Exception as e:
            logging.error(f"Failed to send message: {e}")
            return "Couldn't reach out to Google Gemini. Try Again..."

    def get_chat_title(self) -> str:
        try:
            response = self.chat.send_message("Write a one-line short title up to 10 words for this conversation in plain text.")
            return response.text.strip()
        except:
            return "New Conversation"

    def get_chat_history(self):
        # Convert history to a serializable format (list of dicts)
        serializable_history = []
        for message in self.chat.history:
            role = message.role
            parts = []
            for part in message.parts:
                if hasattr(part, 'text'):
                    parts.append({'text': part.text})
                # We skip images in saved history for now to keep it small in DB
            serializable_history.append({'role': role, 'parts': parts})
        return serializable_history

    @staticmethod
    def list_uploaded_files(api_key: str = None) -> List[Dict[str, Any]]:
        """List all files currently uploaded to Gemini API."""
        try:
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

    def close(self) -> None:
        logging.info("Closed model instance")
        self.chat = None
        self.chat_history = []

    def generate_image(self, prompt: str) -> str:
        """Generate an image using Imagen model if supported by API."""
        try:
            # Imagen is often under a different model name like 'imagen-3.0-generate-001'
            # For now, we'll try to find a model that supports image generation 
            # or use a common default if available.
            # However, standard Gemini 1.5 doesn't generate images directly via generate_content.
            # We'll use a specialized method if the SDK supports it.
            # If not directly supported, we might need to use Vertex AI or another endpoint.
            # For this implementation, we will assume the use of a generative model that supports image output.
            
            # Since Gemini 1.5 doesn't currently generate images directly in the same way it generates text
            # (it's multimodal input, not yet multimodal output in this SDK version typically),
            # we'll use a placeholder or log that this requires Imagen API.
            
            # Update: Some versions of the API allow using 'imagen' model.
            model = genai.GenerativeModel("imagen-3.0-generate-001") # Example model name
            response = model.generate_content(prompt)
            # This would return an image object.
            # Since we can't easily handle raw image bytes here without knowing the exact SDK response,
            # we will provide a structured way for the bot to call it.
            return response
        except Exception as e:
            logger.error(f"Failed to generate image: {e}")
            raise

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
            # Clean JSON
            json_str = response.text.strip().replace("```json", "").replace("```", "")
            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to parse voice command: {e}")
            return {"action": "none", "parameters": {}}