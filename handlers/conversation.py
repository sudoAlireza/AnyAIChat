"""Conversation handlers: start_conversation, reply_and_new_message.

Refactored from bot/conversation_handlers.py to use the ChatSession abstraction
and the provider-agnostic error hierarchy.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import uuid

import httpx
import PIL.Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from handlers.common import restricted, _, _get_pool, _clear_last_ai_buttons, _current_user_id, get_api_key
from handlers.states import CONVERSATION, CHOOSING
from chat.session import ChatSession
from config import (
    MAX_MESSAGE_LENGTH,
    CONVERSATION_WARNING_THRESHOLD,
    CONVERSATION_AUTO_RESET_THRESHOLD,
)
from database.database import (
    get_user,
    select_conversation_by_id,
    create_conversation,
    get_user_knowledge,
    record_token_usage_with_provider,
)
from helpers.helpers import strip_markdown, split_message
from helpers.sanitize import safe_filename
from providers.base import (
    ProviderError,
    RateLimitError,
    InsufficientQuotaError,
    ServiceUnavailableError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# start_conversation (New_Conversation callback)
# ---------------------------------------------------------------------------

@restricted
async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt the user to begin or continue a conversation."""
    query = update.callback_query
    await query.answer()

    logger.info("Received callback: New_Conversation")

    conv_id = context.user_data.get("conversation_id")
    if conv_id:
        message_content = _("\U0001f4ac Continuing conversation. Send your message.")
    else:
        message_content = _("\U0001f4ac New conversation started. Send your first message.")

    keyboard = [[InlineKeyboardButton(_("\U0001f519 Menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message_content, reply_markup=reply_markup)

    return CONVERSATION


# ---------------------------------------------------------------------------
# reply_and_new_message (main chat loop)
# ---------------------------------------------------------------------------

@restricted
async def reply_and_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send user message to the AI provider and respond."""
    message = update.message
    if not message:
        return CONVERSATION

    text = message.text or message.caption
    if text and len(text) > MAX_MESSAGE_LENGTH:
        await message.reply_text(_("Message is too long."))
        return CONVERSATION

    # Send typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await message.reply_text("\u23f3 ...")

    file_path = None
    try:
        pool = _get_pool(context)
        user_id = update.effective_user.id

        chat_session: ChatSession | None = context.user_data.get("chat_session")

        if not chat_session:
            conv_id = context.user_data.get("conversation_id")
            history: list[dict] = []
            if conv_id:
                conv_data = await select_conversation_by_id(pool, (user_id, conv_id))
                if conv_data and conv_data.get("history"):
                    history = json.loads(conv_data["history"])

            user_data = await get_user(pool, user_id)
            model_name = (
                user_data.get("model_name")
                if user_data
                else context.user_data.get("model_name")
            )
            system_instruction = (
                user_data.get("system_instruction")
                if user_data
                else context.user_data.get("system_instruction")
            )
            pinned_context = user_data.get("pinned_context") if user_data else None
            user_language = user_data.get("language", "auto") if user_data else "auto"
            knowledge = await get_user_knowledge(pool, user_id)

            thinking_mode = user_data.get("thinking_mode", "off") if user_data else "off"
            code_exec = user_data.get("code_execution", False) if user_data else False

            api_key = await get_api_key(context, user_id)
            provider_name = context.user_data.get("active_provider", "gemini")

            chat_session = ChatSession(
                provider_name=provider_name,
                api_key=api_key,
                model_name=model_name,
                history=history,
                system_instruction=system_instruction,
                knowledge_base=knowledge,
                pinned_context=pinned_context,
                language=user_language,
                pool=pool,
                user_id=user_id,
                thinking_mode=thinking_mode,
                code_execution=code_exec,
                web_search=bool(context.user_data.get("web_search")),
            )
            await chat_session.start_chat()
            context.user_data["chat_session"] = chat_session

        # ---- Handle multimodal inputs ----
        image = None
        file_mime_type = None
        prompt = text

        if message.photo:
            photo = message.photo[-1]
            photo_file = await photo.get_file()
            buf = io.BytesIO()
            await photo_file.download_to_memory(buf)
            buf.seek(0)
            image = PIL.Image.open(buf)
            if not prompt:
                prompt = "Describe this image"

        elif message.voice:
            voice = message.voice
            file = await context.bot.get_file(voice.file_id)
            file_path = safe_filename(voice.file_id, "voice.ogg", prefix="voice")
            await file.download_to_drive(file_path)

            # Voice-to-Action: parse command from audio without extra API call
            try:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action="typing",
                )
                command = await chat_session.parse_voice_command_from_file(file_path)
                logger.info(f"Voice Command Parsed: {command}")

                if command.get("action") == "set_reminder":
                    params = command.get("parameters", {})
                    remind_text = params.get("text", "Reminder")
                    remind_time = params.get("time", "in 1 hour")
                    await update.message.reply_text(
                        f"Voice command detected: Set reminder for '{remind_text}' at {remind_time}. "
                        "Please confirm in Reminders menu."
                    )
                elif command.get("action") == "generate_image":
                    img_prompt = command.get("parameters", {}).get("prompt")
                    if img_prompt:
                        await msg.edit_text(_("\U0001f3a8 Generating image..."))
                        # Delegate to generate_image_handler (imported lazily to avoid circular deps)
                        from handlers.media import generate_image_handler
                        return await generate_image_handler(update, context, img_prompt)
            except Exception as ve:
                logger.error(f"Voice to action error: {ve}")

            file_mime_type = "audio/ogg"
            if not prompt:
                prompt = "Please transcribe and answer this voice message."
            else:
                prompt = f"Please transcribe and answer this voice message. Additional text: {text}"

        elif message.document:
            doc = message.document
            file = await context.bot.get_file(doc.file_id)
            file_path = safe_filename(doc.file_id, doc.file_name, prefix="doc")
            await file.download_to_drive(file_path)
            file_mime_type = doc.mime_type
            if not prompt:
                prompt = "Summarize this document"

        # ---- CSV pre-processing ----
        if file_path and file_mime_type and (
            "csv" in (file_mime_type or "")
            or (file_path and file_path.endswith(".csv"))
        ):
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    reader = csv.reader(f)
                    rows = list(reader)
                if rows:
                    headers = rows[0] if rows else []
                    data_rows = rows[1:] if len(rows) > 1 else []
                    csv_summary = f"CSV file with {len(data_rows)} rows and {len(headers)} columns.\n"
                    csv_summary += f"Columns: {', '.join(headers[:20])}\n"
                    if data_rows:
                        csv_summary += "Sample (first 3 rows):\n"
                        for row in data_rows[:3]:
                            csv_summary += f"  {', '.join(row[:10])}\n"
                    prompt = f"{prompt or 'Analyze this data'}\n\n[CSV Data Summary]\n{csv_summary}"
            except Exception as csv_err:
                logger.warning(f"CSV pre-processing failed: {csv_err}")

        # ---- URL summarization ----
        if prompt and not image and not file_path:
            url_match = re.search(r'https?://[^\s<>"{}|\\^`\[\]]+', prompt)
            if url_match:
                url = url_match.group(0)
                try:
                    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                            from bs4 import BeautifulSoup

                            soup = BeautifulSoup(resp.text, "html.parser")
                            for tag in soup(["script", "style", "nav", "footer", "header"]):
                                tag.decompose()
                            page_text = soup.get_text(separator="\n", strip=True)[:3000]
                            prompt = f"{prompt}\n\n[Fetched page content from {url}]\n{page_text}"
                except Exception as url_err:
                    logger.warning(f"URL fetch failed: {url_err}")

        if not prompt and not image and not file_path:
            await msg.edit_text(_(
                "No content to process. Send a message, photo, voice, or document."
            ))
            return CONVERSATION

        # Re-send typing for long operations
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # ---- Send to AI (streaming with fallback) ----
        usage: dict | None = None
        grounding_sources: list[dict] = []

        try:
            async def _stream_update(partial: str) -> None:
                try:
                    await msg.edit_text(partial + " \u25cd")
                except BadRequest:
                    pass

            response = await chat_session.send_message_streaming(
                prompt,
                on_update=_stream_update,
                image=image,
                file_path=file_path,
                file_mime_type=file_mime_type,
            )
        except Exception as stream_err:
            logger.warning(f"Streaming failed, falling back to regular: {stream_err}")
            response = await chat_session.send_message(
                prompt,
                image=image,
                file_path=file_path,
                file_mime_type=file_mime_type,
            )

        response_text = response.text
        usage = response.usage
        grounding_sources = response.sources

        # Record token usage (including cached and thinking tokens)
        provider_name = chat_session.provider_name
        if usage:
            await record_token_usage_with_provider(
                pool,
                user_id,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
                model_name=chat_session.model_name,
                provider=provider_name,
                cached_tokens=usage.get("cached_tokens", 0),
                thinking_tokens=usage.get("thinking_tokens", 0),
                estimated_cost_usd=None,
            )

        # Show thinking indicator if thinking tokens were used
        thinking_tokens = usage.get("thinking_tokens", 0) if usage else 0
        if thinking_tokens > 0:
            response_text = f"_\U0001f4ad Reasoning ({thinking_tokens:,} tokens)_\n\n{response_text}"

        # Append provider indicator
        from chat.formatters import format_usage_summary
        usage_summary = format_usage_summary(usage, provider=provider_name) if usage else ""
        if usage_summary:
            response_text += f"\n\n_{usage_summary}_"

        # Append grounding sources
        if grounding_sources:
            seen_uris: set[str] = set()
            sources_text = "\n\n*Sources:*"
            for src in grounding_sources:
                if src["uri"] not in seen_uris:
                    seen_uris.add(src["uri"])
                    title = src.get("title") or src["uri"]
                    sources_text += f"\n\u2022 [{title}]({src['uri']})"
            response_text += sources_text

        # ---- Conversation length warning / auto-save ----
        history_length = chat_session.get_history_length()
        if history_length >= CONVERSATION_AUTO_RESET_THRESHOLD:
            response_text += (
                "\n\n\u26a0\ufe0f This conversation has become very long. "
                "It has been auto-saved. Please start a new conversation for best performance."
            )
            # Auto-save
            history = chat_session.get_history_as_dicts()
            title = await chat_session.get_chat_title()
            conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"
            await create_conversation(pool, (conv_id, user_id, title, json.dumps(history)))
            context.user_data["chat_session"] = None
            context.user_data["conversation_id"] = None
        elif history_length >= CONVERSATION_WARNING_THRESHOLD:
            response_text += (
                "\n\n\u26a0\ufe0f This conversation is getting long. "
                "Consider starting a new conversation for better performance."
            )

        # Store last response for voice/bookmark features
        context.user_data["last_ai_response"] = response_text

        # ---- Build response keyboard ----
        keyboard = [
            [
                InlineKeyboardButton(_("\U0001f4be Save & Menu"), callback_data="Start_Again_SAVE_CONV"),
                InlineKeyboardButton(_("\U0001f519 Menu"), callback_data="Start_Again_CONV"),
            ],
            [
                InlineKeyboardButton("\U0001f4a1", callback_data="Suggest_Followup"),
                InlineKeyboardButton("\U0001f50a", callback_data="Voice_Output"),
                InlineKeyboardButton("\u2b50", callback_data="Bookmark_Msg"),
                InlineKeyboardButton("\U0001f44d", callback_data="Feedback_Up"),
                InlineKeyboardButton("\U0001f44e", callback_data="Feedback_Down"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        parts = split_message(response_text)

        # Clear buttons from previous AI response before sending new one
        await _clear_last_ai_buttons(context)

        # Edit the "processing" message with the first part (keeps it persistent)
        last_btn_msg = msg  # track which message gets the buttons
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            markup = reply_markup if is_last else None

            if i == 0:
                # Edit the placeholder message with the first response part
                try:
                    await msg.edit_text(
                        text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
                    )
                except BadRequest:
                    await msg.edit_text(text=strip_markdown(part), reply_markup=markup)
            else:
                # Send additional parts as new messages
                try:
                    sent = await update.message.reply_text(
                        text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
                    )
                except BadRequest:
                    sent = await update.message.reply_text(
                        text=strip_markdown(part), reply_markup=markup,
                    )
                if is_last:
                    last_btn_msg = sent

        # Store the message with buttons so we can clear them later
        context.user_data["last_ai_message_id"] = last_btn_msg.message_id
        context.user_data["last_ai_chat_id"] = last_btn_msg.chat_id

    except InsufficientQuotaError:
        await msg.edit_text(_(
            "\u26a0\ufe0f API quota exceeded. Your API key has hit its rate limit.\n\n"
            "You can:\n"
            "\u2022 Wait a minute and try again\n"
            "\u2022 Check your quota at your provider's dashboard\n"
            "\u2022 Upgrade your API plan for higher limits"
        ))
    except RateLimitError:
        await msg.edit_text(_(
            "\u26a0\ufe0f Rate limit exceeded. Please wait a moment and try again."
        ))
    except ServiceUnavailableError:
        await msg.edit_text(_(
            "\u26a0\ufe0f Service temporarily unavailable. Please try again in a moment."
        ))
    except ProviderError as e:
        logger.error(f"Provider error: {e}")
        await msg.edit_text(_(
            "\u26a0\ufe0f Failed to get a response. Please try again."
        ))
    except Exception as e:
        logger.error(f"Error in reply_and_new_message: {e}", exc_info=True)
        await msg.edit_text(_("\u274c An unexpected error occurred. Please try again."))
    finally:
        # Always clean up temp files
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"Failed to clean up temp file {file_path}: {e}")

    return CONVERSATION
