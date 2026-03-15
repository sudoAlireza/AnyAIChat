import io
import os
import re
import logging
import uuid
import math
import json
import asyncio
import contextvars
import google.generativeai as genai
from functools import wraps
from datetime import datetime

# Context variable to propagate user_id into all log records within a handler
_current_user_id = contextvars.ContextVar('current_user_id', default='-')

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest
from tenacity import RetryError
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable
import PIL.Image

from core import GeminiChat
from config import (
    DATABASE_PATH, MAX_MESSAGE_LENGTH, ITEMS_PER_PAGE,
    AUTHORIZED_USER, ALLOW_ALL_USERS, GEMINI_API_TOKEN,
    GEMINI_MODEL, CONVERSATION_WARNING_THRESHOLD,
    CONVERSATION_AUTO_RESET_THRESHOLD,
)
from database.database import (
    create_conversation,
    get_user_conversation_count,
    select_conversations_by_user,
    select_conversation_by_id,
    delete_conversation_by_id,
    create_task,
    get_user_tasks,
    delete_task_by_id,
    get_user,
    update_user_api_key,
    update_user_settings,
    add_knowledge,
    get_user_knowledge,
    delete_knowledge,
    add_reminder,
    get_user_reminders,
    delete_reminder,
    get_pending_reminders,
    update_reminder_status,
)
from helpers.inline_paginator import InlineKeyboardPaginator
from helpers.helpers import conversations_page_content, strip_markdown, split_message, escape_markdown_v2
from helpers.sanitize import safe_filename
from security.rate_limiter import rate_limiter
from monitoring.metrics import metrics

# Translation function placeholder (will be set by main.py)
def _(text):
    import builtins
    if '_' in builtins.__dict__:
        return builtins.__dict__['_'](text)
    return text

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

CHOOSING, CONVERSATION, CONVERSATION_HISTORY, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_TIME, TASKS_ADD_INTERVAL, TASKS_CONFIRM_PLAN, SETTINGS_MENU, MODELS_MENU, STORAGE_MENU, API_KEY_INPUT, PERSONA_MENU, PERSONA_INPUT, REMINDERS_MENU, REMINDERS_INPUT, KNOWLEDGE_MENU, KNOWLEDGE_INPUT = range(18)

# Global reference to scheduler and application for task scheduling
_scheduler = None
_application = None

def set_scheduler(scheduler, application):
    global _scheduler, _application
    _scheduler = scheduler
    _application = application


def _get_pool(context):
    """Get database pool from bot_data."""
    return context.bot_data["db_pool"]


def restricted(func):
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id

        # Phase 1.6: Make open-access explicit
        if not AUTHORIZED_USER:
            if not ALLOW_ALL_USERS:
                logger.warning(f"Access denied for {user_id}: AUTHORIZED_USER not set and ALLOW_ALL_USERS is false")
                if update.message:
                    await update.message.reply_text("Bot is not configured for public access. Contact the administrator.")
                elif update.callback_query:
                    await update.callback_query.answer("Bot not configured for public access.", show_alert=True)
                return
            # ALLOW_ALL_USERS is true, let everyone in
        else:
            authorized_users = [int(u.strip()) for u in AUTHORIZED_USER.split(',') if u.strip()]
            if authorized_users and user_id not in authorized_users:
                logger.info(f"Unauthorized access denied for {user_id}.")
                if update.message:
                    await update.message.reply_text("This is a personal GeminiBot. You are not authorized.")
                elif update.callback_query:
                    await update.callback_query.answer("Unauthorized.", show_alert=True)
                return

        # Phase 1.5: Rate limiting
        if not rate_limiter.is_allowed(user_id):
            wait_time = rate_limiter.get_wait_time(user_id)
            msg = f"Rate limit exceeded. Please wait {int(wait_time)} seconds."
            if update.message:
                await update.message.reply_text(msg)
            elif update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            metrics.increment("rate_limit_hits")
            return

        metrics.increment("messages_processed")
        _current_user_id.set(str(user_id))
        return await func(update, context, *args, **kwargs)

    return wrapped


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation with /start command and ask the user for input."""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or "unknown"
    logger.info(f"/start from {username} (id={user_id})")
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    if not user or not user.get('api_key'):
        await update.message.reply_text(_(
            "Welcome! To use this bot, you need to provide your own Gemini API Key.\n\n"
            "How to get your API key:\n"
            "1. Go to aistudio.google.com\n"
            "2. Sign in with your Google account\n"
            "3. Click \"Get API Key\" in the left sidebar\n"
            "4. Click \"Create API Key\" and copy it\n\n"
            "Video tutorial: https://youtu.be/RVGbLSVFtIk?t=22\n\n"
            "Please paste your API Key below:"
        ))
        return API_KEY_INPUT

    # Sync context with DB settings
    context.user_data["api_key"] = user['api_key']
    context.user_data["model_name"] = user['model_name']
    context.user_data["web_search"] = bool(user['grounding'])
    context.user_data["system_instruction"] = user.get('system_instruction')

    keyboard = [
        [InlineKeyboardButton(_("💬 New Conversation"), callback_data="New_Conversation")],
        [
            InlineKeyboardButton(_("📂 History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("📋 Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("⏰ Reminders"), callback_data="Reminders_Menu"),
            InlineKeyboardButton(_("📚 Knowledge"), callback_data="Knowledge_Menu"),
        ],
        [InlineKeyboardButton(_("⚙️ Settings"), callback_data="Settings_Menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = _("✨ *Gemini Chat Bot*\n\nAsk me anything — text, voice, photos, or documents.")

    if update.message:
        try:
            await update.message.reply_text(text=welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await update.message.reply_text(text=strip_markdown(welcome_text), reply_markup=reply_markup)
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(text=welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await update.callback_query.edit_message_text(text=strip_markdown(welcome_text), reply_markup=reply_markup)

    return CHOOSING


@restricted
async def handle_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the user's API key after validation."""
    api_key = update.message.text.strip()
    user_id = update.effective_user.id

    # Phase 1.2: Validate API key format
    # Strip control characters
    api_key = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', api_key)

    if not re.match(r'^AIza[A-Za-z0-9_-]{35,}$', api_key):
        await update.message.reply_text(_("Invalid API key format. Gemini API keys start with 'AIza' and are about 39 characters long. Please try again:"))
        return API_KEY_INPUT

    # Validate key with a lightweight API call
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        models = await GeminiChat.async_list_models(api_key=api_key)
        if not models:
            await update.message.reply_text(_("API key validation failed. The key doesn't seem to work. Please check and try again:"))
            return API_KEY_INPUT
    except Exception as e:
        logger.warning(f"API key validation error for user {user_id}: {e}")
        await update.message.reply_text(_("Could not validate API key. Please check and try again:"))
        return API_KEY_INPUT

    pool = _get_pool(context)
    await update_user_api_key(pool, user_id, api_key)

    await update.message.reply_text(_("API Key saved successfully! Now you can start using the bot."))
    return await start(update, context)


@restricted
async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Close current chat and return to main menu."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    pool = _get_pool(context)

    # Save conversation if requested
    gemini_chat = context.user_data.get("gemini_chat")
    if gemini_chat and query and "_SAVE" in query.data:
        history = gemini_chat.get_chat_history()
        title = await gemini_chat.async_get_chat_title()
        conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"

        await create_conversation(pool, (conv_id, user_id, title, json.dumps(history)))
        logger.info(f"Conversation {conv_id} saved for user {user_id}")

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(_("Conversation saved successfully!"))

        # Clean up context
        context.user_data["gemini_chat"] = None
        context.user_data["gemini_image_chat"] = None
        context.user_data["conversation_id"] = None

        return await start_menu_new_message(update, context)

    # Clean up context
    context.user_data["gemini_chat"] = None
    context.user_data["gemini_image_chat"] = None
    context.user_data["conversation_id"] = None

    # Refresh user data from DB
    user = await get_user(pool, user_id)
    if user:
        context.user_data["api_key"] = user['api_key']
        context.user_data["model_name"] = user['model_name']
        context.user_data["web_search"] = bool(user['grounding'])
        context.user_data["system_instruction"] = user.get('system_instruction')

    return await start(update, context)


async def start_menu_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send main menu as a new message."""
    keyboard = [
        [InlineKeyboardButton(_("💬 New Conversation"), callback_data="New_Conversation")],
        [
            InlineKeyboardButton(_("📂 History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("📋 Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("⏰ Reminders"), callback_data="Reminders_Menu"),
            InlineKeyboardButton(_("📚 Knowledge"), callback_data="Knowledge_Menu"),
        ],
        [InlineKeyboardButton(_("⚙️ Settings"), callback_data="Settings_Menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = _("✨ *Gemini Chat Bot*\n\nAsk me anything — text, voice, photos, or documents.")

    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=strip_markdown(welcome_text), reply_markup=reply_markup)
    return CHOOSING


@restricted
async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user to start conversation."""
    query = update.callback_query
    await query.answer()

    logger.info("Received callback: New_Conversation")

    conv_id = context.user_data.get("conversation_id")
    if conv_id:
        message_content = _("💬 Continuing conversation. Send your message.")
    else:
        message_content = _("💬 New conversation started. Send your first message.")

    keyboard = [[InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message_content, reply_markup=reply_markup)

    return CONVERSATION


@restricted
async def reply_and_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send user message to Gemini and respond."""
    message = update.message
    if not message:
        return CONVERSATION

    text = message.text or message.caption
    if text and len(text) > MAX_MESSAGE_LENGTH:
        await message.reply_text(_("Message is too long."))
        return CONVERSATION

    # Phase 6.1: Send typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await message.reply_text("⏳ ...")

    file_path = None
    try:
        pool = _get_pool(context)
        gemini_chat = context.user_data.get("gemini_chat")
        user_id = update.effective_user.id
        if not gemini_chat:
            conv_id = context.user_data.get("conversation_id")
            history = []
            if conv_id:
                conv_data = await select_conversation_by_id(pool, (user_id, conv_id))
                if conv_data and conv_data.get('history'):
                    history = json.loads(conv_data['history'])

            user_data = await get_user(pool, user_id)
            model_name = user_data.get('model_name') if user_data else context.user_data.get("model_name")
            system_instruction = user_data.get('system_instruction') if user_data else context.user_data.get("system_instruction")
            knowledge = await get_user_knowledge(pool, user_id)

            tools = []
            if context.user_data.get("web_search"):
                tools.append("google_search")

            api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
            gemini_chat = GeminiChat(api_key, chat_history=history, model_name=model_name, tools=tools, system_instruction=system_instruction, knowledge_base=knowledge)
            await gemini_chat.async_start_chat()
            context.user_data["gemini_chat"] = gemini_chat

        # Handle Multimodal Inputs
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

            # Voice-to-Action Implementation
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
                transcription_response = await gemini_chat.async_send_message(
                    "Please transcribe this audio exactly as it is, without any other text.",
                    file_path=file_path, file_mime_type="audio/ogg"
                )

                command = await gemini_chat.async_parse_voice_command(transcription_response)
                logger.info(f"Voice Command Parsed: {command}")

                if command.get('action') == 'set_reminder':
                    params = command.get('parameters', {})
                    remind_text = params.get('text', 'Reminder')
                    remind_time = params.get('time', 'in 1 hour')
                    await update.message.reply_text(f"Voice command detected: Set reminder for '{remind_text}' at {remind_time}. Please confirm in Reminders menu.")
                elif command.get('action') == 'generate_image':
                    img_prompt = command.get('parameters', {}).get('prompt')
                    if img_prompt:
                        await msg.edit_text(_("🎨 Generating image..."))
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

        if not prompt and not image and not file_path:
             await msg.edit_text(_("No content to process. Send a message, photo, voice, or document."))
             return CONVERSATION

        # Phase 6.1: Re-send typing for long operations
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        response_text = await gemini_chat.async_send_message(prompt, image=image, file_path=file_path, file_mime_type=file_mime_type)

        # Phase 4.3: Conversation length warning
        history_length = gemini_chat.get_history_length()
        if history_length >= CONVERSATION_AUTO_RESET_THRESHOLD:
            response_text += "\n\n⚠️ This conversation has become very long. It has been auto-saved. Please start a new conversation for best performance."
            # Auto-save
            history = gemini_chat.get_chat_history()
            title = await gemini_chat.async_get_chat_title()
            conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"
            await create_conversation(pool, (conv_id, user_id, title, json.dumps(history)))
            context.user_data["gemini_chat"] = None
            context.user_data["conversation_id"] = None
        elif history_length >= CONVERSATION_WARNING_THRESHOLD:
            response_text += "\n\n⚠️ This conversation is getting long. Consider starting a new conversation for better performance."

        keyboard = [
            [
                InlineKeyboardButton(_("💾 Save & Menu"), callback_data="Start_Again_SAVE"),
                InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        parts = split_message(response_text)

        # Edit the "processing" message with the first part (keeps it persistent)
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            markup = reply_markup if is_last else None

            if i == 0:
                # Edit the placeholder message with the first response part
                try:
                    await msg.edit_text(text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
                except BadRequest:
                    await msg.edit_text(text=strip_markdown(part), reply_markup=markup)
            else:
                # Send additional parts as new messages
                try:
                    await update.message.reply_text(text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
                except BadRequest:
                    await update.message.reply_text(text=strip_markdown(part), reply_markup=markup)

    except RetryError as e:
        root = e.last_attempt.exception() if e.last_attempt else e
        if isinstance(root, ResourceExhausted):
            logger.warning(f"Gemini quota exceeded for user: {root}")
            await msg.edit_text(
                _("⚠️ Gemini API quota exceeded. Your API key has hit its rate limit.\n\n"
                  "You can:\n"
                  "• Wait a minute and try again\n"
                  "• Check your quota at ai.google.dev\n"
                  "• Upgrade your API plan for higher limits")
            )
        elif isinstance(root, ServiceUnavailable):
            logger.warning(f"Gemini service unavailable: {root}")
            await msg.edit_text(
                _("⚠️ Gemini API is temporarily unavailable. Please try again in a moment.")
            )
        else:
            logger.error(f"Gemini retry exhausted: {e}", exc_info=True)
            await msg.edit_text(_("⚠️ Failed to get a response from Gemini after multiple attempts. Please try again later."))
    except (ResourceExhausted, ServiceUnavailable) as e:
        logger.warning(f"Gemini API error: {e}")
        if isinstance(e, ResourceExhausted):
            await msg.edit_text(
                _("⚠️ Gemini API quota exceeded. Please wait a moment and try again.")
            )
        else:
            await msg.edit_text(
                _("⚠️ Gemini API is temporarily unavailable. Please try again in a moment.")
            )
    except Exception as e:
        logger.error(f"Error in reply_and_new_message: {e}", exc_info=True)
        await msg.edit_text(_("❌ An unexpected error occurred. Please try again."))
    finally:
        # Phase 4.2: Always clean up temp files
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"Failed to clean up temp file {file_path}: {e}")

    return CONVERSATION


@restricted
async def get_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Retrieve a specific conversation via inline button or typed command."""
    try:
        # Support both inline button (callback_query) and typed /convXXX command
        if update.callback_query:
            query = update.callback_query
            await query.answer()
            conv_id = query.data.split("#")[1]
        else:
            conv_id = update.message.text.strip().replace("/", "")

        user_id = update.effective_user.id
        pool = _get_pool(context)

        conversation = await select_conversation_by_id(pool, (user_id, conv_id))
        if not conversation:
            msg = _("Conversation not found.")
            if update.callback_query:
                await update.callback_query.edit_message_text(msg)
            else:
                await update.message.reply_text(msg)
            return CONVERSATION_HISTORY

        context.user_data["conversation_id"] = conv_id

        # Build a detail card
        title = conversation.get('title', 'Untitled')
        msg_count = 0
        last_exchange = ""
        history_raw = conversation.get('history')
        if history_raw:
            try:
                history = json.loads(history_raw)
                msg_count = len(history)
                # Show last 2 exchanges as preview
                recent = []
                for entry in history[-4:]:
                    role = entry.get('role', '')
                    parts = entry.get('parts', [])
                    text_parts = [p.get('text', '') for p in parts if p.get('text')]
                    if text_parts:
                        preview_text = text_parts[0][:100]
                        if len(text_parts[0]) > 100:
                            preview_text += "..."
                        emoji = "👤" if role == "user" else "🤖"
                        recent.append(f"  {emoji} {preview_text}")
                if recent:
                    last_exchange = "\n".join(recent)
            except (json.JSONDecodeError, TypeError):
                pass

        detail = f"📂 *{title}*\n"
        detail += "━" * 24 + "\n\n"
        detail += f"📊 Messages: {msg_count}\n"
        if last_exchange:
            detail += f"\n*Last messages:*\n{last_exchange}\n"
        detail += "\n" + "━" * 24

        keyboard = [
            [InlineKeyboardButton(_("▶️ Continue Conversation"), callback_data="New_Conversation")],
            [
                InlineKeyboardButton(_("🗑 Delete"), callback_data="Delete_Conversation"),
                InlineKeyboardButton(_("📋 Back to List"), callback_data="PAGE#1"),
            ],
            [InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text=detail, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.callback_query.edit_message_text(
                    text=strip_markdown(detail), reply_markup=reply_markup
                )
        else:
            try:
                await update.message.reply_text(
                    text=detail, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
                )
            except BadRequest:
                await update.message.reply_text(
                    text=strip_markdown(detail), reply_markup=reply_markup
                )
    except Exception as e:
        logger.error(f"Error in get_conversation_handler: {e}", exc_info=True)
    return CONVERSATION_HISTORY


@restricted
async def delete_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete current conversation."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if conv_id:
        pool = _get_pool(context)
        await delete_conversation_by_id(pool, (update.effective_user.id, conv_id))
        await query.edit_message_text(_("Deleted. Back to menu."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Menu"), callback_data="Start_Again")]]))
    return CHOOSING


@restricted
async def get_conversation_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """List conversations with inline selection buttons."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    page_number = int(query.data.split("#")[1])
    pool = _get_pool(context)

    count = await get_user_conversation_count(pool, user_id)
    total_pages = math.ceil(count / ITEMS_PER_PAGE) if count > 0 else 1

    conversations = await select_conversations_by_user(pool, (user_id, (page_number - 1) * ITEMS_PER_PAGE))

    if not conversations:
        keyboard = [
            [InlineKeyboardButton(_("➕ Start New Conversation"), callback_data="New_Conversation")],
            [InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")],
        ]
        await query.edit_message_text(
            _("💬 No conversations yet.\n\nStart a new conversation and save it to see it here."),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CONVERSATION_HISTORY

    content = conversations_page_content(conversations)

    # Build conversation selection buttons
    keyboard = []
    for conv in conversations:
        title = conv.get('title', 'Untitled')
        if len(title) > 35:
            title = title[:32] + "..."
        msg_count = conv.get('message_count', 0)
        keyboard.append([InlineKeyboardButton(
            f"💬 {title} ({msg_count} msgs)",
            callback_data=f"CONV_SELECT#{conv['conversation_id']}"
        )])

    # Pagination row
    nav_buttons = []
    if page_number > 1:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"PAGE#{page_number - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"📄 {page_number}/{total_pages}", callback_data="noop"))
    if page_number < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"PAGE#{page_number + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")])

    try:
        await query.edit_message_text(
            text=content,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest:
        await query.edit_message_text(
            text=strip_markdown(content),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return CONVERSATION_HISTORY


# --- Tasks Handlers ---

@restricted
async def open_tasks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(_("➕ Add New Task"), callback_data="Tasks_Add")],
        [InlineKeyboardButton(_("📋 List Tasks"), callback_data="Tasks_List")],
        [InlineKeyboardButton(_("🔙 Back to Main Menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(_("Tasks Menu"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_MENU

@restricted
async def start_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Menu")]]
    await query.edit_message_text(_("Enter task prompt:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_PROMPT

@restricted
async def handle_task_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(_("Enter task prompt:"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")]]))
        return TASKS_ADD_PROMPT

    context.user_data["task_prompt"] = update.message.text

    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Prompt")]]
    await update.message.reply_text(_("Enter time (HH:MM):"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_TIME

@restricted
async def handle_task_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text
    try:
        datetime.strptime(time_str, "%H:%M")
        context.user_data["task_time"] = time_str

        keyboard = [
            [InlineKeyboardButton(_("Once"), callback_data="Tasks_Interval_once")],
            [InlineKeyboardButton(_("Daily"), callback_data="Tasks_Interval_daily")],
            [InlineKeyboardButton(_("Weekly"), callback_data="Tasks_Interval_weekly")],
            [InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Prompt")],
        ]
        await update.message.reply_text(_("Choose interval:"), reply_markup=InlineKeyboardMarkup(keyboard))
        return TASKS_ADD_INTERVAL
    except ValueError:
        await update.message.reply_text(_("Invalid format. Use HH:MM:"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Prompt")]]))
        return TASKS_ADD_TIME

@restricted
async def handle_task_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    interval = query.data.split("_")[-1]

    user_id = update.effective_user.id
    prompt = context.user_data.get("task_prompt")
    run_time = context.user_data.get("task_time")

    # Generate Plan
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await query.edit_message_text(_("Generating plan..."))
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
    gemini = GeminiChat(api_key)
    plan_json_str = await gemini.async_generate_plan(prompt)
    context.user_data["task_plan"] = plan_json_str
    context.user_data["task_interval"] = interval

    try:
        plan = json.loads(plan_json_str)
        if not isinstance(plan, list):
             raise ValueError("Plan is not a list")

        text = f"📋 *30-Day Plan: {prompt[:40]}*\n"
        text += f"⏰ {run_time} | 🔄 {interval}\n"
        text += "━" * 25 + "\n\n"

        current_phase = None
        for day in plan:
            phase = day.get('phase', '')
            if phase and phase != current_phase:
                current_phase = phase
                text += f"\n📌 *{phase}*\n\n"

            day_num = day.get('day', '?')
            title = day.get('title', '')
            subject = day.get('subject', '')

            if day_num in (7, 14, 21, 30):
                text += f"  🏁 Day {day_num}: *{title}*\n"
            else:
                text += f"  📅 Day {day_num}: *{title}*\n"
            text += f"        {subject}\n"

        text += "\n" + "━" * 25
        text += _("\n\nDo you approve this plan?")

        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:3900] + "\n...\n\n_(Plan truncated for display, full plan will be saved)_" + _("\n\nDo you approve this plan?")
        keyboard = [
            [InlineKeyboardButton(_("✅ Approve"), callback_data="Plan_Approve")],
            [InlineKeyboardButton(_("❌ Reject"), callback_data="Plan_Reject")],
            [InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Time")],
        ]
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
        return TASKS_CONFIRM_PLAN
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse plan: {e}. Plan: {plan_json_str}")
        await query.edit_message_text(_("Failed to generate a valid plan. Try again."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Add")]]))
        return TASKS_MENU

@restricted
async def back_to_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(_("Once"), callback_data="Tasks_Interval_once")],
        [InlineKeyboardButton(_("Daily"), callback_data="Tasks_Interval_daily")],
        [InlineKeyboardButton(_("Weekly"), callback_data="Tasks_Interval_weekly")],
        [InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Prompt")],
    ]
    await query.edit_message_text(_("Choose interval:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_INTERVAL

@restricted
async def handle_task_plan_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "Plan_Reject":
        await query.edit_message_text(_("Plan rejected. Let's start over."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")]]))
        return TASKS_MENU

    user_id = update.effective_user.id
    prompt = context.user_data.get("task_prompt")
    run_time = context.user_data.get("task_time")
    interval = context.user_data.get("task_interval")
    plan_json = context.user_data.get("task_plan")

    if plan_json.startswith("```"):
        lines = plan_json.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines[-1].startswith("```"):
            lines = lines[:-1]
        plan_json = "\n".join(lines).strip()

    start_date = datetime.now().strftime("%Y-%m-%d")

    pool = _get_pool(context)
    task_id = await create_task(pool, (user_id, prompt, run_time, interval, plan_json, start_date))

    schedule_task_job(task_id, user_id, prompt, run_time, interval, plan_json, start_date)

    await query.edit_message_text(_("Task scheduled!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]))
    return TASKS_MENU

@restricted
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    tasks = await get_user_tasks(pool, update.effective_user.id)
    if not tasks:
        await query.edit_message_text(_("No tasks."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")]]))
        return TASKS_MENU

    text = _("Your tasks:\n")
    keyboard = []
    for t in tasks:
        text += f"ID: {t['id']} | {t['run_time']} | {t['interval']} | {t['prompt'][:20]}...\n"
        keyboard.append([InlineKeyboardButton(_(f"Delete Task #{t['id']}"), callback_data=f"TASK_DELETE#{t['id']}")])

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_MENU

@restricted
async def delete_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    if await delete_task_by_id(pool, (update.effective_user.id, task_id)):
        if _scheduler:
            try:
                _scheduler.remove_job(str(task_id))
            except Exception as e:
                logger.warning(f"Failed to remove scheduler job: {e}")
        await query.edit_message_text(_("Task deleted."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to List"), callback_data="Tasks_List")]]))
    return TASKS_MENU

def schedule_task_job(task_id, user_id, prompt, run_time, interval, plan_json=None, start_date=None):
    if not _scheduler:
        return

    hour, minute = map(int, run_time.split(":"))

    async def task_wrapper():
        target_prompt = prompt

        if plan_json and start_date:
            try:
                plan = json.loads(plan_json)
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                days_passed = (datetime.now() - start_dt).days + 1

                day_item = next((item for item in plan if item['day'] == days_passed), None)
                if day_item:
                    phase = day_item.get('phase', '')
                    phase_info = f" Phase: {phase}." if phase else ""
                    target_prompt = (
                        f"You are delivering Day {days_passed}/30 of a structured learning plan.{phase_info}\n"
                        f"Today's title: {day_item['title']}\n"
                        f"Today's goal: {day_item['subject']}\n"
                        f"Overall topic: {prompt}\n\n"
                        f"Provide today's content in a clear, engaging format. "
                        f"Start with a brief recap connection to yesterday, then deliver today's material. "
                        f"End with a quick action item or reflection question."
                    )
                else:
                    target_prompt = f"Plan finished or day {days_passed} not found. Original prompt: {prompt}"
            except Exception as e:
                logger.error(f"Error in task_wrapper plan processing: {e}")

        pool = _application.bot_data.get("db_pool")
        if pool:
            user = await get_user(pool, user_id)
        else:
            user = None
        api_key = user.get('api_key') if user else GEMINI_API_TOKEN
        model_name = user.get('model_name') if user else None
        tools = ["google_search"] if user and user.get('grounding') else []

        gemini = GeminiChat(api_key, model_name=model_name, tools=tools)
        await gemini.async_start_chat()
        response = await gemini.async_send_message(target_prompt)
        header = f"📬 *Daily Task: {prompt[:50]}*\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        parts = split_message(header + response)
        for part in parts:
            try:
                await _application.bot.send_message(chat_id=user_id, text=part, parse_mode=ParseMode.MARKDOWN)
            except BadRequest as e:
                logger.error(f"Failed to send task result with markdown: {e}")
                await _application.bot.send_message(chat_id=user_id, text=strip_markdown(part))

    job_id = str(task_id)
    if interval == "once":
        _scheduler.add_job(task_wrapper, 'cron', hour=hour, minute=minute, id=job_id, replace_existing=True)
    elif interval == "daily":
        _scheduler.add_job(task_wrapper, 'cron', hour=hour, minute=minute, id=job_id, replace_existing=True)
    elif interval == "weekly":
        _scheduler.add_job(task_wrapper, 'cron', day_of_week='mon', hour=hour, minute=minute, id=job_id, replace_existing=True)

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_over(update, context)


# --- Settings Handlers ---

@restricted
async def open_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    logger.info(f"Opening settings menu for user {user_id}")

    current_model = user['model_name'] if user and user.get('model_name') else (context.user_data.get("model_name") or GEMINI_MODEL)
    web_search = bool(user['grounding']) if user and user.get('grounding') is not None else context.user_data.get("web_search", False)

    ws_status = "✅ Enabled" if web_search else "❌ Disabled"

    keyboard = [
        [InlineKeyboardButton(f"🤖 Model: {current_model}", callback_data="open_models_menu")],
        [InlineKeyboardButton(f"🎭 Custom Persona", callback_data="Persona_Menu")],
        [InlineKeyboardButton(f"🌐 Web Search: {ws_status}", callback_data="TOGGLE_WEB_SEARCH")],
        [InlineKeyboardButton(_("📁 Storage Management"), callback_data="Storage_Menu")],
        [InlineKeyboardButton(_("🔑 Update API Key"), callback_data="UPDATE_API_KEY")],
        [InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")],
    ]

    try:
        await query.edit_message_text(_("Settings Menu"), reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")
            await context.bot.send_message(chat_id=user_id, text=_("Settings Menu"), reply_markup=InlineKeyboardMarkup(keyboard))

    return SETTINGS_MENU


@restricted
async def update_api_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    logger.info(f"Initiating API key update for user {user_id}")

    await query.edit_message_text(_(
        "🔑 Update API Key\n\n"
        "How to get your API key:\n"
        "1. Go to aistudio.google.com\n"
        "2. Sign in with your Google account\n"
        "3. Click \"Get API Key\" in the left sidebar\n"
        "4. Click \"Create API Key\" and copy it\n\n"
        "Video tutorial: https://youtu.be/RVGbLSVFtIk?t=22\n\n"
        "Please paste your new API Key below:"
    ))
    return API_KEY_INPUT

@restricted
async def open_models_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show a menu of all available models from Google AI."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return MODELS_MENU

    user_id = update.effective_user.id
    logger.info(f"Opening models menu for user {user_id}")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
    models = await GeminiChat.async_list_models(api_key=api_key)
    if not models:
        await query.edit_message_text(_("Failed to fetch models or no models available."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]))
        return SETTINGS_MENU

    current_model = context.user_data.get("model_name") or GEMINI_MODEL

    keyboard = []
    for m in models:
        prefix = "✅ " if m['name'].endswith(current_model) or m['name'] == current_model else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{m['display_name']}", callback_data=f"SET_MODEL_{m['name']}")])

    keyboard.append([InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(_("Choose a Gemini Model:"), reply_markup=InlineKeyboardMarkup(keyboard))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")

    return MODELS_MENU

@restricted
async def set_model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return MODELS_MENU

    model_name = query.data.replace("SET_MODEL_", "")
    user_id = update.effective_user.id
    logger.info(f"Setting model to {model_name} for user {user_id}")

    pool = _get_pool(context)
    await update_user_settings(pool, user_id, model_name=model_name)
    context.user_data["model_name"] = model_name

    await open_models_menu(update, context)
    return MODELS_MENU

@restricted
async def open_storage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show files currently stored in Gemini's temporary storage."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return STORAGE_MENU

    user_id = update.effective_user.id
    logger.info(f"Opening storage menu for user {user_id}")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
    files = await GeminiChat.async_list_uploaded_files(api_key=api_key)
    if not files:
        content = _("No files currently stored in Gemini's temporary storage.")
    else:
        content = _("Active files in Google's temporary storage (expire after 48h):\n\n")
        for f in files:
            size_mb = f['size_bytes'] / (1024 * 1024)
            content += f"• `{f['display_name']}` ({f['mime_type']}, {size_mb:.2f} MB)\n"

    keyboard = [[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]

    try:
        await query.edit_message_text(content, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")

    return STORAGE_MENU


# --- Persona, Reminders, Knowledge, Image Handlers ---

@restricted
async def open_persona_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)
    current_persona = user.get('system_instruction') or "Default (Female Assistant)"

    text = f"Your Current Persona:\n\n{current_persona}\n\nEnter a new system instruction/persona if you want to change it."
    keyboard = [[InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")]]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return PERSONA_INPUT

@restricted
async def handle_persona_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    persona_text = update.message.text
    user_id = update.effective_user.id
    pool = _get_pool(context)
    await update_user_settings(pool, user_id, system_instruction=persona_text)
    context.user_data["system_instruction"] = persona_text

    await update.message.reply_text(_("Persona updated successfully!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Settings"), callback_data="Settings_Menu")]]))
    return SETTINGS_MENU

@restricted
async def open_reminders_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    reminders = await get_user_reminders(pool, update.effective_user.id)

    text = "⏰ Your Reminders:\n\n"
    keyboard = [[InlineKeyboardButton(_("➕ Add Reminder"), callback_data="Add_Reminder")]]

    if reminders:
        for r in reminders[:10]:
            status = "✅" if r['status'] == 'completed' else "⏳"
            text += f"{status} {r['remind_at']}: {r['reminder_text']}\n"
            keyboard.append([InlineKeyboardButton(_(f"Delete Reminder #{r['id']}"), callback_data=f"REMINDER_DELETE#{r['id']}")])
    else:
        text += "No reminders found."

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Main Menu"), callback_data="Start_Again")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return REMINDERS_MENU

@restricted
async def start_add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(_("Please enter your reminder in format: YYYY-MM-DD HH:MM | Reminder text\nExample: 2026-03-20 15:30 | Call Mom"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Reminders_Menu")]]))
    return REMINDERS_INPUT

@restricted
async def handle_reminder_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    try:
        time_part, msg_part = [s.strip() for s in text.split('|')]
        datetime.strptime(time_part, "%Y-%m-%d %H:%M")

        user_id = update.effective_user.id
        pool = _get_pool(context)
        await add_reminder(pool, (user_id, msg_part, time_part))

        await update.message.reply_text(_("Reminder saved!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Reminders"), callback_data="Reminders_Menu")]]))
        return REMINDERS_MENU
    except (ValueError, IndexError) as e:
        logger.warning(f"Invalid reminder format from user: {e}")
        await update.message.reply_text(_("Invalid format. Use YYYY-MM-DD HH:MM | Reminder text"))
        return REMINDERS_INPUT

@restricted
async def delete_reminder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    reminder_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_reminder(pool, update.effective_user.id, reminder_id)
    await open_reminders_menu(update, context)
    return REMINDERS_MENU

@restricted
async def open_knowledge_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    knowledge = await get_user_knowledge(pool, update.effective_user.id)

    text = "📚 Your Knowledge Base (RAG):\n\n"
    keyboard = [[InlineKeyboardButton(_("➕ Add Document"), callback_data="Add_Knowledge")]]

    if knowledge:
        for doc in knowledge:
            text += f"• {doc['file_name']}\n"
            keyboard.append([InlineKeyboardButton(_(f"Delete {doc['file_name']}"), callback_data=f"KNOWLEDGE_DELETE#{doc['id']}")])
    else:
        text += "No documents uploaded. These documents will be used as context for all your conversations."

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Main Menu"), callback_data="Start_Again")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return KNOWLEDGE_MENU

@restricted
async def start_add_knowledge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(_("Please upload a document (PDF or Text) that you want to add to your knowledge base."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Knowledge_Menu")]]))
    return KNOWLEDGE_INPUT

@restricted
async def handle_knowledge_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.document:
        await update.message.reply_text(_("Please upload a document."))
        return KNOWLEDGE_INPUT

    doc = update.message.document
    file = await context.bot.get_file(doc.file_id)
    file_path = safe_filename(doc.file_id, doc.file_name, prefix="rag")
    await file.download_to_drive(file_path)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
    gemini = GeminiChat(api_key)
    try:
        loop = asyncio.get_event_loop()
        uploaded_file = await loop.run_in_executor(None, lambda: genai.upload_file(path=file_path, mime_type=doc.mime_type))
        model = gemini._get_model()
        response = await loop.run_in_executor(None, lambda: model.generate_content(["Summarize this document in 2-3 sentences to be used as context for future queries.", uploaded_file]))
        preview = response.text.strip()

        pool = _get_pool(context)
        await add_knowledge(pool, (update.effective_user.id, doc.file_name, doc.file_id, preview))

        await update.message.reply_text(_("Document added to Knowledge Base!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Knowledge Menu"), callback_data="Knowledge_Menu")]]))
    except Exception as e:
        logger.error(f"Failed to process RAG document: {e}")
        await update.message.reply_text(_("Failed to process document. Make sure it's a valid text or PDF."))
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"Failed to clean up RAG temp file: {e}")

    return KNOWLEDGE_MENU

@restricted
async def delete_knowledge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    doc_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_knowledge(pool, update.effective_user.id, doc_id)
    await open_knowledge_menu(update, context)
    return KNOWLEDGE_MENU

@restricted
async def generate_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str = None) -> int:
    """Handle image generation command."""
    if not prompt:
        if update.message and "/image" in update.message.text:
             prompt = update.message.text.replace("/image", "").strip()

    if not prompt:
        await update.message.reply_text(_("Please provide a prompt for the image. Example: /image a beautiful sunset"))
        return CONVERSATION

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await update.message.reply_text("🎨 ...")

    try:
        api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
        gemini = GeminiChat(api_key)
        response = await gemini.async_generate_image(prompt)

        await msg.edit_text(_("🎨 Image generation requested for: ") + prompt + _("\n\n(Note: Imagen API integration is experimental and may require specific account permissions)"))
    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        await msg.edit_text(_("❌ Failed to generate image. ") + str(e))

    return CONVERSATION

async def check_reminders_task():
    """Background task to check for due reminders."""
    if not _application:
        return

    pool = _application.bot_data.get("db_pool")
    if not pool:
        return

    reminders = await get_pending_reminders(pool)

    for r in reminders:
        try:
            await _application.bot.send_message(chat_id=r['user_id'], text=f"⏰ REMINDER: {r['reminder_text']}")
            await update_reminder_status(pool, r['id'], 'completed')
        except Exception as e:
            logger.error(f"Failed to send reminder: {e}")

@restricted
async def toggle_web_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    current = context.user_data.get("web_search", False)
    new_status = not current

    user_id = update.effective_user.id
    logger.info(f"Toggling web search to {new_status} for user {user_id}")

    pool = _get_pool(context)
    await update_user_settings(pool, user_id, grounding=int(new_status))
    context.user_data["web_search"] = new_status

    await open_settings_menu(update, context)
    return SETTINGS_MENU
