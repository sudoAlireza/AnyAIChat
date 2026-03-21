import io
import os
import re
import csv
import hashlib
import logging
import uuid
import math
import json
import asyncio
import contextvars
from functools import wraps
from datetime import datetime, timedelta
import httpx

# Context variable to propagate user_id into all log records within a handler
_current_user_id = contextvars.ContextVar('current_user_id', default='-')

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
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
    search_conversations,
    add_conversation_tag,
    get_user_tags,
    get_conversations_by_tag,
    get_conversation_tags,
    remove_conversation_tag,
    add_shortcut,
    get_user_shortcuts,
    delete_shortcut,
    get_shortcut_by_command,
    get_user_stats,
    add_bookmark,
    get_user_bookmarks,
    delete_bookmark,
    add_prompt,
    get_user_prompts,
    delete_prompt,
    add_feedback,
    mark_task_completed,
    get_task_by_id,
    get_user_task_hashtags,
    delete_task_by_hashtag,
    _generate_hashtag,
    add_url_monitor,
    get_user_monitors,
    delete_url_monitor,
    get_active_monitors,
    update_monitor_hash,
    update_conversation_resume,
    create_conversation_branch,
    record_token_usage,
    get_user_token_stats,
    add_knowledge_with_content,
    delete_chunks_by_knowledge_id,
    save_knowledge_chunks,
)
from schemas import REMINDER_SCHEMA
from helpers.code_formatter import format_code_blocks_for_telegram
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

CHOOSING, CONVERSATION, CONVERSATION_HISTORY, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_DAYS, TASKS_ADD_TIME, TASKS_ADD_INTERVAL, TASKS_CONFIRM_PLAN, SETTINGS_MENU, MODELS_MENU, STORAGE_MENU, API_KEY_INPUT, PERSONA_MENU, PERSONA_INPUT, REMINDERS_MENU, REMINDERS_INPUT, KNOWLEDGE_MENU, KNOWLEDGE_INPUT, SEARCH_INPUT, SHORTCUTS_MENU, SHORTCUTS_INPUT, TAGS_INPUT, PINNED_CONTEXT_INPUT, TEMPLATES_MENU, BOOKMARKS_MENU, PROMPT_LIBRARY, PROMPT_ADD, BRIEFING_MENU, URL_MONITOR_MENU, URL_MONITOR_INPUT = range(31)

# Conversation templates
CONVERSATION_TEMPLATES = [
    {"id": "code_review", "name": "💻 Code Review", "persona": "You are an expert code reviewer. Analyze code for bugs, performance, security, and best practices. Be specific and constructive."},
    {"id": "email_writer", "name": "✉️ Email Writer", "persona": "You are a professional email writer. Help compose clear, concise, and appropriate emails. Ask for context if needed."},
    {"id": "brainstorm", "name": "🧠 Brainstorm", "persona": "You are a creative brainstorming partner. Generate diverse ideas, ask probing questions, and think outside the box."},
    {"id": "summarizer", "name": "📝 Summarizer", "persona": "You are a summarization expert. Create clear, concise summaries while preserving key information. Use bullet points."},
    {"id": "tutor", "name": "🎓 Tutor", "persona": "You are a patient tutor. Explain concepts clearly, use analogies, and adapt to the student's level."},
    {"id": "writer", "name": "✍️ Creative Writer", "persona": "You are a creative writer. Help with stories, articles, blog posts. Match the desired tone and style."},
    {"id": "researcher", "name": "🔍 Researcher", "persona": "You are a thorough research assistant. Provide well-structured, fact-based information with caveats."},
    {"id": "debugger", "name": "🐛 Debug Helper", "persona": "You are a debugging expert. Help identify and fix bugs. Explain root causes and suggest preventive measures."},
]

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

        _current_user_id.set(str(user_id))
        return await func(update, context, *args, **kwargs)

    return wrapped


async def _clear_last_ai_buttons(context: ContextTypes.DEFAULT_TYPE):
    """Remove inline buttons from the previous AI response message."""
    last = context.user_data.pop("last_ai_message_id", None)
    chat = context.user_data.pop("last_ai_chat_id", None)
    if last and chat:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat, message_id=last, reply_markup=None
            )
        except BadRequest:
            pass


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation with /start command and ask the user for input."""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name or "unknown"
    logger.info(f"/start from {username} (id={user_id})")

    # Clear pending task message buttons
    pending = context.bot_data.get("pending_task_buttons", {})
    task_btn = pending.pop(user_id, None)
    if task_btn:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=task_btn[0], message_id=task_btn[1], reply_markup=None
            )
        except BadRequest:
            pass

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

    # Handle deep-link: /start discuss_{task_id}_{day_num}
    if context.args and context.args[0].startswith("discuss_"):
        try:
            parts = context.args[0].split("_")
            dl_task_id = int(parts[1])
            dl_day_num = int(parts[2])
            task = await get_task_by_id(pool, dl_task_id)
            if task and task["user_id"] == user_id:
                plan = json.loads(task["plan_json"]) if task.get("plan_json") else []
                day_item = next((item for item in plan if item["day"] == dl_day_num), None)
                if day_item:
                    sys_instr = (
                        f"You are a knowledgeable tutor helping the user discuss Day {dl_day_num} of their learning plan.\n"
                        f"Topic: {task['prompt']}\n"
                        f"Today's title: {day_item['title']}\n"
                        f"Today's subject: {day_item['subject']}\n\n"
                        "Answer questions, provide examples, go deeper into the topic, or clarify concepts. "
                        "Be conversational, helpful, and encourage curiosity."
                    )
                    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
                    model_name = context.user_data.get("model_name")
                    tools = ["google_search"] if context.user_data.get("web_search") else []
                    gemini = GeminiChat(api_key, model_name=model_name, tools=tools, system_instruction=sys_instr)
                    await gemini.start_chat()
                    context.user_data["gemini_chat"] = gemini
                    context.user_data["conversation_id"] = None

                    keyboard = [[InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again")]]
                    await update.message.reply_text(
                        f"💬 Let's discuss *Day {dl_day_num}: {day_item['title']}*\n\nSend your question.",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return CONVERSATION
        except (ValueError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Failed to handle discuss deep-link: {e}")

    keyboard = [
        [
            InlineKeyboardButton(_("💬 New Chat"), callback_data="New_Conversation"),
            InlineKeyboardButton(_("📝 Templates"), callback_data="Templates_Menu"),
        ],
        [
            InlineKeyboardButton(_("📂 History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("📋 Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("⏰ Reminders"), callback_data="Reminders_Menu"),
            InlineKeyboardButton(_("📚 Knowledge"), callback_data="Knowledge_Menu"),
        ],
        [
            InlineKeyboardButton(_("🔍 Search"), callback_data="Search_Menu"),
            InlineKeyboardButton(_("⭐ Bookmarks"), callback_data="Bookmarks_Menu"),
        ],
        [
            InlineKeyboardButton(_("📖 Prompts"), callback_data="Prompt_Library"),
            InlineKeyboardButton(_("📊 Usage"), callback_data="Usage_Dashboard"),
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
        models = await GeminiChat.list_models(api_key=api_key)
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

    # Detect if button was pressed on an AI response message (_CONV suffix)
    from_conversation = query and "_CONV" in query.data

    # Remove buttons from the clicked AI response message
    if from_conversation:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass

    # Save conversation if requested
    gemini_chat = context.user_data.get("gemini_chat")
    if gemini_chat and query and "_SAVE" in query.data:
        history = gemini_chat.get_chat_history()
        title = await gemini_chat.get_chat_title()
        conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"

        await create_conversation(pool, (conv_id, user_id, title, json.dumps(history)))
        logger.info(f"Conversation {conv_id} saved for user {user_id}")

        # Auto-tagging: suggest tags based on title
        try:
            auto_tags = []
            title_lower = title.lower()
            tag_keywords = {
                'code': ['code', 'programming', 'debug', 'function', 'api', 'bug', 'error'],
                'writing': ['write', 'essay', 'article', 'story', 'blog', 'email'],
                'math': ['math', 'calculate', 'equation', 'formula', 'number'],
                'science': ['science', 'physics', 'chemistry', 'biology', 'research'],
                'language': ['translate', 'grammar', 'language', 'english', 'spanish'],
                'work': ['meeting', 'project', 'deadline', 'report', 'business'],
                'learning': ['learn', 'study', 'explain', 'tutorial', 'course'],
                'creative': ['creative', 'idea', 'brainstorm', 'design', 'art'],
            }
            for tag, keywords in tag_keywords.items():
                if any(kw in title_lower for kw in keywords):
                    auto_tags.append(tag)
            for tag in auto_tags[:3]:
                await add_conversation_tag(pool, user_id, conv_id, tag)
        except Exception as tag_err:
            logger.warning(f"Auto-tagging failed: {tag_err}")

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_("Conversation saved successfully!")
        )

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

    # AI response buttons (_CONV): always send menu as new message to preserve the response.
    # Menu buttons: edit the current menu message as normal navigation.
    if from_conversation:
        return await start_menu_new_message(update, context)

    return await start(update, context)


async def start_menu_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send main menu as a new message."""
    keyboard = [
        [
            InlineKeyboardButton(_("💬 New Chat"), callback_data="New_Conversation"),
            InlineKeyboardButton(_("📝 Templates"), callback_data="Templates_Menu"),
        ],
        [
            InlineKeyboardButton(_("📂 History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("📋 Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("⏰ Reminders"), callback_data="Reminders_Menu"),
            InlineKeyboardButton(_("📚 Knowledge"), callback_data="Knowledge_Menu"),
        ],
        [
            InlineKeyboardButton(_("🔍 Search"), callback_data="Search_Menu"),
            InlineKeyboardButton(_("⭐ Bookmarks"), callback_data="Bookmarks_Menu"),
        ],
        [
            InlineKeyboardButton(_("📖 Prompts"), callback_data="Prompt_Library"),
            InlineKeyboardButton(_("📊 Usage"), callback_data="Usage_Dashboard"),
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
            pinned_context = user_data.get('pinned_context') if user_data else None
            user_language = user_data.get('language', 'auto') if user_data else 'auto'
            knowledge = await get_user_knowledge(pool, user_id)

            tools = []
            if context.user_data.get("web_search"):
                tools.append("google_search")

            thinking_mode = user_data.get('thinking_mode', 'off') if user_data else 'off'
            code_exec = user_data.get('code_execution', False) if user_data else False

            api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
            gemini_chat = GeminiChat(
                api_key, chat_history=history, model_name=model_name, tools=tools,
                system_instruction=system_instruction, knowledge_base=knowledge,
                pinned_context=pinned_context, language=user_language,
                pool=pool, user_id=user_id,
                thinking_mode=thinking_mode, code_execution=code_exec,
            )
            await gemini_chat.start_chat()
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

            # Voice-to-Action: parse command from audio without extra API call
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
                command = await gemini_chat.parse_voice_command_from_file(file_path)
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

        # CSV Analysis: detect CSV files and pre-process
        if file_path and file_mime_type and ('csv' in (file_mime_type or '') or (file_path and file_path.endswith('.csv'))):
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    reader = csv.reader(f)
                    rows = list(reader)
                if rows:
                    headers = rows[0] if rows else []
                    data_rows = rows[1:] if len(rows) > 1 else []
                    csv_summary = f"CSV file with {len(data_rows)} rows and {len(headers)} columns.\n"
                    csv_summary += f"Columns: {', '.join(headers[:20])}\n"
                    if data_rows:
                        csv_summary += f"Sample (first 3 rows):\n"
                        for row in data_rows[:3]:
                            csv_summary += f"  {', '.join(row[:10])}\n"
                    prompt = f"{prompt or 'Analyze this data'}\n\n[CSV Data Summary]\n{csv_summary}"
            except Exception as csv_err:
                logger.warning(f"CSV pre-processing failed: {csv_err}")

        # URL Summarization: detect URLs and fetch content
        if prompt and not image and not file_path:
            url_match = re.search(r'https?://[^\s<>"{}|\\^`\[\]]+', prompt)
            if url_match:
                url = url_match.group(0)
                try:
                    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                        if resp.status_code == 200 and 'text/html' in resp.headers.get('content-type', ''):
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(resp.text, 'html.parser')
                            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                                tag.decompose()
                            page_text = soup.get_text(separator='\n', strip=True)[:3000]
                            prompt = f"{prompt}\n\n[Fetched page content from {url}]\n{page_text}"
                except Exception as url_err:
                    logger.warning(f"URL fetch failed: {url_err}")

        if not prompt and not image and not file_path:
             await msg.edit_text(_("No content to process. Send a message, photo, voice, or document."))
             return CONVERSATION

        # Phase 6.1: Re-send typing for long operations
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Try streaming first for text-only messages, fall back to regular
        usage = None
        grounding_sources = []
        try:
            async def _stream_update(partial):
                try:
                    await msg.edit_text(partial + " ▍")
                except BadRequest:
                    pass

            response_text, usage, grounding_sources = await gemini_chat.send_message_streaming(
                prompt, on_update=_stream_update, image=image, file_path=file_path, file_mime_type=file_mime_type
            )
        except Exception as stream_err:
            logger.warning(f"Streaming failed, falling back to regular: {stream_err}")
            response_text, usage, grounding_sources = await gemini_chat.send_message(prompt, image=image, file_path=file_path, file_mime_type=file_mime_type)

        # Record token usage (including cached and thinking tokens)
        if usage:
            await record_token_usage(
                pool, user_id, usage["prompt_tokens"], usage["completion_tokens"],
                usage["total_tokens"], model_name=gemini_chat.model_name,
                cached_tokens=usage.get("cached_tokens", 0),
                thinking_tokens=usage.get("thinking_tokens", 0),
            )

        # Phase 3: Show thinking indicator if thinking tokens were used
        thinking_tokens = usage.get("thinking_tokens", 0) if usage else 0
        if thinking_tokens > 0:
            response_text = f"_💭 Reasoning ({thinking_tokens:,} tokens)_\n\n{response_text}"

        # Phase 6: Append grounding sources
        if grounding_sources:
            seen_uris = set()
            sources_text = "\n\n*Sources:*"
            for src in grounding_sources:
                if src["uri"] not in seen_uris:
                    seen_uris.add(src["uri"])
                    title = src.get("title") or src["uri"]
                    sources_text += f"\n• [{title}]({src['uri']})"
            response_text += sources_text

        # Phase 4.3: Conversation length warning
        history_length = gemini_chat.get_history_length()
        if history_length >= CONVERSATION_AUTO_RESET_THRESHOLD:
            response_text += "\n\n⚠️ This conversation has become very long. It has been auto-saved. Please start a new conversation for best performance."
            # Auto-save
            history = gemini_chat.get_chat_history()
            title = await gemini_chat.get_chat_title()
            conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"
            await create_conversation(pool, (conv_id, user_id, title, json.dumps(history)))
            context.user_data["gemini_chat"] = None
            context.user_data["conversation_id"] = None
        elif history_length >= CONVERSATION_WARNING_THRESHOLD:
            response_text += "\n\n⚠️ This conversation is getting long. Consider starting a new conversation for better performance."

        # File Output: detect code blocks and offer as downloadable files
        # code_blocks = re.findall(r'```(\w*)\n(.*?)```', response_text, re.DOTALL)
        # if code_blocks:
        #     ext_map = {
        #         'python': '.py', 'javascript': '.js', 'typescript': '.ts', 'java': '.java',
        #         'go': '.go', 'rust': '.rs', 'cpp': '.cpp', 'c': '.c', 'html': '.html',
        #         'css': '.css', 'sql': '.sql', 'bash': '.sh', 'shell': '.sh',
        #         'yaml': '.yaml', 'json': '.json', 'xml': '.xml', 'ruby': '.rb',
        #         'php': '.php', 'swift': '.swift', 'kotlin': '.kt',
        #     }
        #     for lang, code in code_blocks:
        #         if len(code.strip()) > 50:
        #             ext = ext_map.get(lang.lower(), '.txt') if lang else '.txt'
        #             file_name = f"code_{uuid.uuid4().hex[:6]}{ext}"
        #             file_buf = io.BytesIO(code.strip().encode('utf-8'))
        #             file_buf.name = file_name
        #             try:
        #                 await message.reply_document(
        #                     document=file_buf,
        #                     filename=file_name,
        #                     caption=f"📄 Code output ({lang or 'text'})"
        #                 )
        #             except Exception as fe:
        #                 logger.warning(f"Failed to send code file: {fe}")

        # Store last response for voice/bookmark features
        context.user_data["last_ai_response"] = response_text

        keyboard = [
            [
                InlineKeyboardButton(_("💾 Save & Menu"), callback_data="Start_Again_SAVE_CONV"),
                InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again_CONV"),
            ],
            [
                InlineKeyboardButton("💡", callback_data="Suggest_Followup"),
                InlineKeyboardButton("🔊", callback_data="Voice_Output"),
                InlineKeyboardButton("⭐", callback_data="Bookmark_Msg"),
                InlineKeyboardButton("👍", callback_data="Feedback_Up"),
                InlineKeyboardButton("👎", callback_data="Feedback_Down"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        parts = split_message(response_text)

        # Clear buttons from previous AI response before sending new one
        await _clear_last_ai_buttons(context)

        # Edit the "processing" message with the first part (keeps it persistent)
        last_btn_msg = msg  # track which message gets the buttons
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
                    sent = await update.message.reply_text(text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
                except BadRequest:
                    sent = await update.message.reply_text(text=strip_markdown(part), reply_markup=markup)
                if is_last:
                    last_btn_msg = sent

        # Store the message with buttons so we can clear them later
        context.user_data["last_ai_message_id"] = last_btn_msg.message_id
        context.user_data["last_ai_chat_id"] = last_btn_msg.chat_id

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

        # Get tags for this conversation
        tags = await get_conversation_tags(pool, user_id, conv_id)
        if tags:
            detail += f"\n🏷 Tags: {', '.join(tags)}"

        keyboard = [
            [InlineKeyboardButton(_("▶️ Continue Conversation"), callback_data="New_Conversation")],
            [
                InlineKeyboardButton(_("🏷 Tag"), callback_data="Tag_Conversation"),
                InlineKeyboardButton(_("📤 Export"), callback_data="Export_Conversation"),
                InlineKeyboardButton(_("🔗 Share"), callback_data="Share_Conversation"),
            ],
            [
                InlineKeyboardButton(_("🔀 Branch"), callback_data="Branch_Conversation"),
                InlineKeyboardButton(_("📍 Resume"), callback_data="Set_Resume_Point"),
            ],
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
    await query.edit_message_text(_("Enter task prompt (max 500 chars):\n\nDescribe the topic and any preferences."), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_PROMPT

@restricted
async def handle_task_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(_("Enter task prompt (max 500 chars):\n\nDescribe the topic and any preferences."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")]]))
        return TASKS_ADD_PROMPT

    TASK_PROMPT_LIMIT = 500
    prompt_text = update.message.text.strip()
    if len(prompt_text) > TASK_PROMPT_LIMIT:
        await update.message.reply_text(
            _(f"Prompt is too long ({len(prompt_text)} chars). Maximum is {TASK_PROMPT_LIMIT} characters.\n\n"
              "Include the topic and key preferences, but keep background concise."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")]]),
        )
        return TASKS_ADD_PROMPT

    context.user_data["task_prompt"] = prompt_text

    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Menu")]]
    await update.message.reply_text(_("How many days should this plan span? (7-60):"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_DAYS

@restricted
async def handle_task_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        num_days = int(text)
        if num_days < 7 or num_days > 60:
            raise ValueError("Out of range")
    except ValueError:
        await update.message.reply_text(
            _("Please enter a number between 7 and 60:"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Menu")]]),
        )
        return TASKS_ADD_DAYS

    context.user_data["task_days"] = num_days

    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Days")]]
    await update.message.reply_text(
        _("Enter time (HH:MM) in UTC, or with timezone (e.g. 11:00 +05:00):"),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TASKS_ADD_TIME

@restricted
async def back_to_days_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Menu")]]
    await query.edit_message_text(_("How many days should this plan span? (7-60):"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_DAYS

@restricted
async def handle_task_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text.strip()
    match = re.match(r'^(\d{2}:\d{2})\s*([+-]\d{2}:\d{2})?$', time_str)
    if not match:
        await update.message.reply_text(
            _("Invalid format. Use HH:MM or HH:MM +HH:MM (e.g. 14:00 +03:30):"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Days")]]),
        )
        return TASKS_ADD_TIME

    time_part = match.group(1)
    tz_offset = match.group(2)

    try:
        dt = datetime.strptime(time_part, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            _("Invalid time. Use HH:MM (e.g. 14:00):"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Days")]]),
        )
        return TASKS_ADD_TIME

    if tz_offset:
        sign = 1 if tz_offset[0] == '+' else -1
        off_h, off_m = map(int, tz_offset[1:].split(":"))
        offset_minutes = sign * (off_h * 60 + off_m)
        total_minutes = dt.hour * 60 + dt.minute - offset_minutes
        total_minutes = total_minutes % (24 * 60)  # wrap around midnight
        utc_time = f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    else:
        utc_time = time_part

    context.user_data["task_time"] = utc_time
    context.user_data["task_selected_days"] = set()

    keyboard = _build_days_keyboard(set())
    await update.message.reply_text(_("Choose which days to run:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_INTERVAL


_ALL_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_LABELS = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}

def _normalize_interval(interval: str) -> str:
    """Convert legacy 'daily'/'weekly'/'once' to day-of-week format."""
    if interval in ("daily", "once"):
        return ",".join(_ALL_DAYS)
    if interval == "weekly":
        return "mon"
    return interval

def _format_interval(interval: str) -> str:
    """Format interval string for display (e.g. 'mon,wed,fri' -> 'Mon, Wed, Fri')."""
    interval = _normalize_interval(interval)
    days = interval.split(",")
    if set(days) == set(_ALL_DAYS):
        return "Every Day"
    return ", ".join(_DAY_LABELS.get(d, d) for d in days)

def _build_days_keyboard(selected: set) -> list:
    """Build inline keyboard with day-of-week toggle buttons."""
    row1, row2 = [], []
    for i, day in enumerate(_ALL_DAYS):
        check = "✅ " if day in selected else ""
        btn = InlineKeyboardButton(f"{check}{_DAY_LABELS[day]}", callback_data=f"Tasks_Day_{day}")
        if i < 4:
            row1.append(btn)
        else:
            row2.append(btn)
    all_selected = selected == set(_ALL_DAYS)
    all_label = "✅ " + _("Every Day") if all_selected else _("Every Day")
    return [
        row1,
        row2,
        [InlineKeyboardButton(all_label, callback_data="Tasks_Day_all")],
        [InlineKeyboardButton(_("✅ Confirm"), callback_data="Tasks_Interval_confirm")],
        [InlineKeyboardButton(_("🔙 Back"), callback_data="Back_To_Days")],
    ]

@restricted
async def handle_day_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle a day-of-week selection or select all."""
    query = update.callback_query
    await query.answer()

    selected = context.user_data.get("task_selected_days", set())
    day = query.data.split("_")[-1]  # e.g. "mon" or "all"

    if day == "all":
        if selected == set(_ALL_DAYS):
            selected = set()
        else:
            selected = set(_ALL_DAYS)
    else:
        if day in selected:
            selected.discard(day)
        else:
            selected.add(day)

    context.user_data["task_selected_days"] = selected
    keyboard = _build_days_keyboard(selected)
    await query.edit_message_text(_("Choose which days to run:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_ADD_INTERVAL


@restricted
async def handle_task_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected = context.user_data.get("task_selected_days", set())
    if not selected:
        await query.answer(_("Please select at least one day."), show_alert=True)
        return TASKS_ADD_INTERVAL

    # Store as sorted comma-separated string: "mon,wed,fri"
    interval = ",".join(d for d in _ALL_DAYS if d in selected)
    user_id = update.effective_user.id
    prompt = context.user_data.get("task_prompt")
    run_time = context.user_data.get("task_time")

    num_days = context.user_data.get("task_days", 30)

    # Generate Plan (structured output — returns parsed dict directly)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    msg = await query.edit_message_text(_("Generating plan..."))
    api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
    gemini = GeminiChat(api_key)
    parsed = await gemini.generate_plan(prompt, num_days=num_days)

    context.user_data["task_interval"] = interval

    try:
        ai_title = parsed.get("title", "")
        plan = parsed.get("plan", [])

        if not isinstance(plan, list) or not plan:
            raise ValueError("Plan is not a valid list")

        # Clean title for hashtag: remove non-alphanumeric, ensure CamelCase
        ai_title = re.sub(r'[^a-zA-Z0-9]', '', ai_title)
        context.user_data["task_hashtag"] = f"#{ai_title}" if ai_title else ""
        context.user_data["task_plan"] = json.dumps(plan)

        preview_hashtag = context.user_data.get("task_hashtag", "")
        text = _build_plan_text(plan, prompt, run_time, interval, preview_hashtag, num_days)
        text += _("\n\nDo you approve this plan?")
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
    except (KeyError, ValueError, AttributeError) as e:
        logger.error(f"Failed to parse plan: {e}")
        await query.edit_message_text(_("Failed to generate a valid plan. Try again."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Add")]]))
        return TASKS_MENU

@restricted
async def back_to_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected = context.user_data.get("task_selected_days", set())
    keyboard = _build_days_keyboard(selected)
    await query.edit_message_text(_("Choose which days to run:"), reply_markup=InlineKeyboardMarkup(keyboard))
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

    now = datetime.now()
    run_hour, run_minute = map(int, run_time.split(":"))
    scheduled_today = now.replace(hour=run_hour, minute=run_minute, second=0, microsecond=0)
    if now >= scheduled_today:
        start_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_date = now.strftime("%Y-%m-%d")

    pool = _get_pool(context)

    # Use AI-generated hashtag from plan generation, fallback to keyword-based
    hashtag = context.user_data.get("task_hashtag", "")
    existing_tags = await get_user_task_hashtags(pool, user_id)
    if not hashtag or hashtag.lower() in {t.lower() for t in existing_tags}:
        hashtag = _generate_hashtag(prompt, existing_tags)

    task_id = await create_task(pool, (user_id, prompt, run_time, interval, plan_json, start_date, hashtag))

    schedule_task_job(task_id, user_id, prompt, run_time, interval, plan_json, start_date, hashtag)

    # Edit the approval message into the plan (keeps it in its original position above)
    num_days = context.user_data.get("task_days", 30)
    try:
        plan = json.loads(plan_json)
        plan_text = _build_plan_text(plan, prompt, run_time, interval, hashtag, num_days)
        try:
            await query.edit_message_text(plan_text, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await query.edit_message_text(strip_markdown(plan_text))
    except (json.JSONDecodeError, ValueError):
        await query.edit_message_text(f"{hashtag}")

    # Send the confirmation as a new message below the plan
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=_("✅ Task scheduled!") + f" {hashtag}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]),
    )

    return TASKS_MENU

def _build_plan_text(plan, prompt, run_time, interval, hashtag, num_days=None):
    """Build the plan preview text used in task view and persistent message."""
    if num_days is None:
        num_days = len(plan)
    milestones = {num_days // 4, num_days // 2, 3 * num_days // 4, num_days}
    total_days = len(plan)
    TELEGRAM_LIMIT = 4096
    RESERVE = 200

    text = f"📋 *{num_days}-Day Plan* {hashtag}\n"
    text += f"📝 _{prompt[:60]}_\n"
    text += f"⏰ {run_time} UTC | 🔄 {_format_interval(interval)}\n"
    text += "━" * 25 + "\n\n"

    current_phase = None
    for day in plan:
        phase = day.get('phase', '')
        if phase and phase != current_phase:
            current_phase = phase
            phase_line = f"\n📌 *{phase}*\n\n"
            if len(text) + len(phase_line) + RESERVE > TELEGRAM_LIMIT:
                day_num = day.get('day', '?')
                remaining = total_days - day_num + 1
                text += f"\n_... and {remaining} more days_\n"
                break
            text += phase_line

        day_num = day.get('day', '?')
        title = day.get('title', '')
        if day_num in milestones:
            line = f"  🏁 Day {day_num}: *{title}*\n"
        else:
            line = f"  📅 Day {day_num}: *{title}*\n"
        if len(text) + len(line) + RESERVE > TELEGRAM_LIMIT:
            remaining = total_days - day_num + 1
            text += f"\n_... and {remaining} more days_\n"
            break
        text += line

    text += "\n" + "━" * 25
    return text


@restricted
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    tasks = await get_user_tasks(pool, update.effective_user.id)
    if not tasks:
        await query.edit_message_text(_("No tasks."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")]]))
        return TASKS_MENU

    text = _("Your tasks:\n\n")
    keyboard = []
    for t in tasks:
        tag = t.get('hashtag') or f"#Task{t['id']}"
        text += f"{tag} | {t['run_time']} | {_format_interval(t['interval'])}\n"
        keyboard.append([InlineKeyboardButton(f"📋 {tag}", callback_data=f"TASK_VIEW#{tag}")])

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Tasks Menu"), callback_data="Tasks_Menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_MENU


@restricted
async def view_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show full plan for a task with delete and back buttons."""
    query = update.callback_query
    await query.answer()
    hashtag = query.data.split("#", 1)[1]

    pool = _get_pool(context)
    tasks = await get_user_tasks(pool, update.effective_user.id)
    task = next((t for t in tasks if (t.get('hashtag') or f"#Task{t['id']}") == hashtag), None)

    if not task:
        await query.edit_message_text(_("Task not found."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to List"), callback_data="Tasks_List")]]))
        return TASKS_MENU

    tag = task.get('hashtag') or f"#Task{task['id']}"
    keyboard = [
        [InlineKeyboardButton(_("🗑 Delete Task"), callback_data=f"TASK_DELETE#{tag}")],
        [InlineKeyboardButton(_("🔙 Back to List"), callback_data="Tasks_List")],
    ]

    plan_json = task.get('plan_json')
    if plan_json:
        try:
            plan = json.loads(plan_json)
            text = _build_plan_text(plan, task['prompt'], task['run_time'], task['interval'], tag)
            try:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            except BadRequest:
                await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
            return TASKS_MENU
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback if no plan or parse error
    text = f"{tag}\n📝 _{task['prompt'][:100]}_\n⏰ {task['run_time']} | 🔄 {_format_interval(task['interval'])}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_MENU


@restricted
async def delete_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hashtag = query.data.split("#", 1)[1]

    pool = _get_pool(context)
    task_id = await delete_task_by_hashtag(pool, update.effective_user.id, hashtag)
    if task_id:
        if _scheduler:
            try:
                _scheduler.remove_job(str(task_id))
            except Exception as e:
                logger.warning(f"Failed to remove scheduler job: {e}")
        await query.edit_message_text(_("Task deleted."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back to List"), callback_data="Tasks_List")]]))
    return TASKS_MENU

def schedule_task_job(task_id, user_id, prompt, run_time, interval, plan_json=None, start_date=None, hashtag=None):
    if not _scheduler:
        return

    interval = _normalize_interval(interval)
    task_hashtag = hashtag or f"#Task{task_id}"
    hour, minute = map(int, run_time.split(":"))

    async def task_wrapper():
        target_prompt = prompt
        days_passed = None
        plan_total = None

        if plan_json and start_date:
            try:
                plan = json.loads(plan_json)
                plan_total = len(plan)
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                # Count scheduled runs: how many selected weekdays from start_date to today (inclusive)
                selected_days = set(interval.split(","))
                day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
                selected_weekdays = {day_map[d] for d in selected_days if d in day_map}
                total_calendar_days = (datetime.now() - start_dt).days
                days_passed = 0
                for i in range(total_calendar_days + 1):
                    check_date = start_dt + timedelta(days=i)
                    if check_date.weekday() in selected_weekdays:
                        days_passed += 1

                day_item = next((item for item in plan if item['day'] == days_passed), None)
                if day_item is None and days_passed > len(plan):
                    # Plan is complete — mark task as done
                    completion_pool = _application.bot_data.get("db_pool")
                    if completion_pool:
                        await mark_task_completed(completion_pool, task_id)
                    try:
                        _scheduler.remove_job(str(task_id))
                    except Exception:
                        pass
                    await _application.bot.send_message(
                        chat_id=user_id,
                        text=f"🎉 *Plan Complete!* {task_hashtag}\nYour {len(plan)}-day plan on _{prompt[:40]}_ has finished.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                elif day_item:
                    phase = day_item.get('phase', '')
                    phase_info = f" Phase: {phase}." if phase else ""
                    target_prompt = (
                        f"You are delivering Day {days_passed}/{len(plan)} of a structured learning plan.{phase_info}\n"
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
        response, usage = await gemini.one_shot(target_prompt)
        if usage and pool:
            await record_token_usage(
                pool, user_id, usage["prompt_tokens"], usage["completion_tokens"],
                usage["total_tokens"], model_name=gemini.model_name,
                cached_tokens=usage.get("cached_tokens", 0),
                thinking_tokens=usage.get("thinking_tokens", 0),
            )
        if days_passed and plan_total:
            header = f"📬 *Day {days_passed}/{plan_total}* {task_hashtag}\n_{prompt[:50]}_\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        else:
            header = f"📬 {task_hashtag}\n_{prompt[:50]}_\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        parts = split_message(header + response)
        # Build buttons for the last message part
        last_markup = None
        bot_username = _application.bot_data.get("bot_username", "")
        buttons = []
        if days_passed and plan_total and bot_username:
            discuss_url = f"https://t.me/{bot_username}?start=discuss_{task_id}_{days_passed}"
            buttons.append(InlineKeyboardButton("💬 Discuss", url=discuss_url))
        if bot_username:
            menu_url = f"https://t.me/{bot_username}?start=menu"
            buttons.append(InlineKeyboardButton("📋 Menu", url=menu_url))
        if buttons:
            last_markup = InlineKeyboardMarkup([buttons])

        last_sent = None
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            markup = last_markup if is_last else None
            try:
                last_sent = await _application.bot.send_message(chat_id=user_id, text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            except BadRequest as e:
                logger.error(f"Failed to send task result with markdown: {e}")
                last_sent = await _application.bot.send_message(chat_id=user_id, text=strip_markdown(part), reply_markup=markup)

        # Store message ID so buttons can be cleared when user opens menu
        if last_sent and last_markup:
            pending = _application.bot_data.setdefault("pending_task_buttons", {})
            pending[user_id] = (last_sent.chat_id, last_sent.message_id)

    job_id = str(task_id)
    # interval is a comma-separated list of day abbreviations, e.g. "mon,wed,fri"
    _scheduler.add_job(task_wrapper, 'cron', day_of_week=interval, hour=hour, minute=minute, id=job_id, replace_existing=True)

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

    user_lang = user.get('language', 'auto') if user else 'auto'

    # Thinking mode status
    thinking_mode = user.get('thinking_mode', 'off') if user else 'off'
    thinking_labels = {"off": "❌ Off", "light": "💡 Light", "medium": "🧠 Medium", "deep": "🔮 Deep"}
    thinking_status = thinking_labels.get(thinking_mode, "❌ Off")

    # Code execution status
    code_exec = user.get('code_execution', False) if user else False
    code_exec_status = "✅ Enabled" if code_exec else "❌ Disabled"

    keyboard = [
        [InlineKeyboardButton(f"🤖 Model: {current_model}", callback_data="open_models_menu")],
        [InlineKeyboardButton(f"🎭 Custom Persona", callback_data="Persona_Menu")],
        [InlineKeyboardButton(f"📌 Pinned Context", callback_data="Pinned_Context_Menu")],
        [InlineKeyboardButton(f"⚡ Quick Shortcuts", callback_data="Shortcuts_Menu")],
        [InlineKeyboardButton(f"🌐 Web Search: {ws_status}", callback_data="TOGGLE_WEB_SEARCH")],
        [InlineKeyboardButton(f"💭 Thinking: {thinking_status}", callback_data="TOGGLE_THINKING_MODE")],
        [InlineKeyboardButton(f"🖥️ Code Execution: {code_exec_status}", callback_data="TOGGLE_CODE_EXEC")],
        [InlineKeyboardButton(f"🌍 Language: {user_lang}", callback_data="Language_Menu")],
        [InlineKeyboardButton(_("🔔 Daily Briefing"), callback_data="Briefing_Menu")],
        [InlineKeyboardButton(_("🔗 URL Monitor"), callback_data="URL_Monitor_Menu")],
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
    models = await GeminiChat.list_models(api_key=api_key)
    if not models:
        await query.edit_message_text(_("Failed to fetch models or no models available."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]))
        return SETTINGS_MENU

    current_model = context.user_data.get("model_name") or GEMINI_MODEL

    # Filter out non-chat models
    skip_patterns = [
        'embedding', 'aqa', 'imagen', 'veo', 'text-',
        'tts', 'image', 'native-audio', 'robotics',
        'computer-use', 'deep-research', 'customtools',
        'nano-banana', 'gemma', 'tuning',
    ]

    def _parse_model_version(name_lower):
        """Extract (major, minor, tier) for sorting. Higher = newer."""
        import re as _re
        # Match gemini-X.Y or gemini-X patterns
        vm = _re.search(r'gemini-(\d+)(?:\.(\d+))?', name_lower)
        if not vm:
            # "latest" aliases without version get sorted high
            if 'latest' in name_lower:
                return (99, 0, 0)
            return (0, 0, 0)
        major = int(vm.group(1))
        minor = int(vm.group(2)) if vm.group(2) else 0
        # Tier: pro > flash > flash-lite/lite, stable > preview > versioned
        if 'pro' in name_lower:
            tier = 3
        elif 'lite' in name_lower:
            tier = 1
        elif 'flash' in name_lower:
            tier = 2
        else:
            tier = 2  # generic/latest aliases
        # Penalize point-release suffixes like -001
        if _re.search(r'-\d{3}$', name_lower):
            tier -= 0.5
        # Penalize preview
        if 'preview' in name_lower:
            tier -= 0.1
        return (major, minor, tier)

    chat_models = []
    for m in models:
        name_lower = m['name'].lower()
        if any(s in name_lower for s in skip_patterns):
            continue
        if not name_lower.startswith('models/gemini'):
            continue
        chat_models.append(m)

    # Sort all chat models: latest version first, then pro > flash > lite
    chat_models.sort(key=lambda m: _parse_model_version(m['name'].lower()), reverse=True)

    # Split into featured (top 8) and others
    featured = chat_models[:8]
    others = chat_models[8:]

    show_all = context.user_data.get("show_all_models", False)

    # Brief description from API or fallback
    desc_map = {
        'pro': '🏆 Pro',
        'flash-lite': '🪶 Lite',
        'flash': '⚡ Flash',
        'lite': '🪶 Lite',
    }

    text = "🤖 *Choose a Gemini Model*\n\n"
    keyboard = []

    display_models = featured if not show_all else featured + others

    for m in display_models:
        is_current = m['name'].endswith(current_model) or m['name'] == current_model
        prefix = "✅ " if is_current else ""

        # Generate short description
        name_lower = m['name'].lower()
        desc = ""
        for key, label in desc_map.items():
            if key in name_lower:
                desc = f" — {label}"
                break
        if m.get('description'):
            short_desc = m['description'][:60]
            if not desc:
                desc = f" — {short_desc}"

        button_text = f"{prefix}{m['display_name']}{desc}"
        if len(button_text) > 60:
            button_text = f"{prefix}{m['display_name']}"

        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"SET_MODEL_{m['name']}")])

    if others and not show_all:
        keyboard.append([InlineKeyboardButton(f"📋 Show all ({len(others)} more)", callback_data="Show_All_Models")])

    keyboard.append([InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
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
async def show_all_models_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle showing all models."""
    query = update.callback_query
    await query.answer()
    context.user_data["show_all_models"] = True
    return await open_models_menu(update, context)


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
    files = await GeminiChat.list_uploaded_files(api_key=api_key)
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
            recurring = f" 🔄{r['recurring_interval']}" if r.get('recurring_interval') else ""
            text += f"{status} {r['remind_at']}: {r['reminder_text']}{recurring}\n"
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
    await query.edit_message_text(
        _("Enter your reminder:\n\n"
          "Standard format: YYYY-MM-DD HH:MM | Reminder text\n"
          "Example: 2026-03-20 15:30 | Call Mom\n\n"
          "Or use natural language:\n"
          "• \"remind me to buy milk tomorrow at 5pm\"\n"
          "• \"call dentist next Monday at 10am\"\n"
          "• \"daily standup every day at 9am\""),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Reminders_Menu")]])
    )
    return REMINDERS_INPUT

@restricted
async def handle_reminder_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return REMINDERS_INPUT
    text = update.message.text
    user_id = update.effective_user.id
    pool = _get_pool(context)
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Reminders"), callback_data="Reminders_Menu")]])

    # First try standard format: YYYY-MM-DD HH:MM | text
    try:
        time_part, msg_part = [s.strip() for s in text.split('|')]
        datetime.strptime(time_part, "%Y-%m-%d %H:%M")
        await add_reminder(pool, (user_id, msg_part, time_part))
        await update.message.reply_text(_("✅ Reminder saved!"), reply_markup=back_btn)
        return REMINDERS_MENU
    except (ValueError, IndexError):
        pass

    # Smart NLP parsing with Gemini (structured output)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        api_key = context.user_data.get("api_key") or GEMINI_API_TOKEN
        gemini = GeminiChat(api_key)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        parse_prompt = (
            f"Parse this reminder request into a structured format. Current time is {now}.\n"
            f"Input: \"{text}\"\n"
            "Extract the reminder text, datetime, and whether it's recurring."
        )
        parsed, _usage = await gemini.one_shot_structured(parse_prompt, REMINDER_SCHEMA)

        if not parsed:
            raise ValueError("Failed to parse structured response")

        remind_text = parsed.get('text', text)
        remind_time = parsed.get('datetime')
        recurring = parsed.get('recurring')

        if not remind_time:
            raise ValueError("No datetime parsed")
        datetime.strptime(remind_time, "%Y-%m-%d %H:%M")

        if recurring:
            await add_reminder(pool, (user_id, remind_text, remind_time, recurring))
        else:
            await add_reminder(pool, (user_id, remind_text, remind_time))

        recurring_text = f" (🔄 {recurring})" if recurring else ""
        await update.message.reply_text(
            f"✅ Reminder set!\n\n📝 {remind_text}\n⏰ {remind_time}{recurring_text}",
            reply_markup=back_btn
        )
        return REMINDERS_MENU
    except Exception as e:
        logger.warning(f"NLP reminder parsing failed: {e}")
        await update.message.reply_text(
            _("Couldn't understand that. Please use format:\nYYYY-MM-DD HH:MM | Reminder text\n\nOr try natural language like 'remind me to call mom tomorrow at 3pm'"),
            reply_markup=back_btn
        )
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
        uploaded_file = await gemini.upload_file(file_path, doc.mime_type)
        preview = await gemini.generate_content_with_file("Summarize this document in 2-3 sentences to be used as context for future queries.", uploaded_file)

        # Read full text content for caching and RAG
        full_content = None
        try:
            full_content = await gemini.generate_content_with_file("Extract and return the full text content of this document verbatim.", uploaded_file)
        except Exception as extract_err:
            logger.warning(f"Failed to extract full content, using preview only: {extract_err}")

        pool = _get_pool(context)
        user_id = update.effective_user.id
        knowledge_id = await add_knowledge_with_content(pool, user_id, doc.file_name, doc.file_id, preview, full_content)

        # Phase 5: Create RAG chunks with embeddings
        if full_content and len(full_content) > 100:
            try:
                from helpers.embeddings import chunk_text, embed_texts
                from config import RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP

                chunks = chunk_text(full_content, chunk_size=RAG_CHUNK_SIZE, overlap=RAG_CHUNK_OVERLAP)
                embeddings = await embed_texts(gemini.client, chunks)

                chunks_data = []
                for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                    chunks_data.append((idx, chunk, json.dumps(embedding)))

                await save_knowledge_chunks(pool, user_id, knowledge_id, chunks_data)
                logger.info(f"Created {len(chunks)} RAG chunks for knowledge doc {knowledge_id}")
            except Exception as rag_err:
                logger.warning(f"RAG chunk creation failed (non-critical): {rag_err}")

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
    await delete_chunks_by_knowledge_id(pool, doc_id)
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
        response = await gemini.generate_image(prompt)

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
            recurring_text = f"\n🔄 Recurring: {r['recurring_interval']}" if r.get('recurring_interval') else ""
            await _application.bot.send_message(chat_id=r['user_id'], text=f"⏰ REMINDER: {r['reminder_text']}{recurring_text}")

            if r.get('recurring_interval'):
                # Schedule next occurrence
                try:
                    current_time = datetime.strptime(r['remind_at'], "%Y-%m-%d %H:%M")
                    if r['recurring_interval'] == 'daily':
                        next_time = current_time + timedelta(days=1)
                    elif r['recurring_interval'] == 'weekly':
                        next_time = current_time + timedelta(weeks=1)
                    else:
                        next_time = None

                    if next_time:
                        await add_reminder(pool, (r['user_id'], r['reminder_text'], next_time.strftime("%Y-%m-%d %H:%M"), r['recurring_interval']))
                except Exception as re:
                    logger.error(f"Failed to schedule recurring reminder: {re}")

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


@restricted
async def toggle_thinking_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cycle through thinking modes: off → light → medium → deep → off."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    current = user.get('thinking_mode', 'off') if user else 'off'
    cycle = ["off", "light", "medium", "deep"]
    next_idx = (cycle.index(current) + 1) % len(cycle) if current in cycle else 0
    new_mode = cycle[next_idx]

    await update_user_settings(pool, user_id, thinking_mode=new_mode)
    # Clear active chat so new settings take effect
    context.user_data["gemini_chat"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU


@restricted
async def toggle_code_execution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle code execution on/off."""
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return SETTINGS_MENU

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    current = user.get('code_execution', False) if user else False
    new_status = not current

    await update_user_settings(pool, user_id, code_execution=new_status)
    # Clear active chat so new settings take effect
    context.user_data["gemini_chat"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU


# --- Search Handlers ---

@restricted
async def search_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open search input."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(_("🏷 Browse by Tag"), callback_data="Browse_Tags")],
        [InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(
        _("🔍 *Search Conversations*\n\nType a keyword to search across all your conversations, or browse by tag."),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return SEARCH_INPUT


@restricted
async def handle_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process search query."""
    search_query = update.message.text.strip()
    if not search_query or len(search_query) < 2:
        await update.message.reply_text(_("Please enter at least 2 characters to search."))
        return SEARCH_INPUT

    pool = _get_pool(context)
    user_id = update.effective_user.id
    results = await search_conversations(pool, user_id, search_query)

    if not results:
        keyboard = [[InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")]]
        await update.message.reply_text(
            _("No conversations found matching your search."),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHOOSING

    text = f"🔍 *Search Results for \"{search_query}\"*\n\n"
    keyboard = []
    for r in results[:10]:
        title = r['title'][:35] + "..." if len(r['title']) > 35 else r['title']
        keyboard.append([InlineKeyboardButton(
            f"💬 {title}",
            callback_data=f"CONV_SELECT#{r['conversation_id']}"
        )])

    keyboard.append([InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")])

    try:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await update.message.reply_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


@restricted
async def browse_tags_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Browse conversations by tag."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    tags = await get_user_tags(pool, user_id)

    if not tags:
        keyboard = [[InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")]]
        await query.edit_message_text(_("No tags found. Tag conversations from the History detail view."), reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING

    keyboard = []
    for tag in tags:
        keyboard.append([InlineKeyboardButton(f"🏷 {tag}", callback_data=f"TAG_BROWSE#{tag}")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")])

    await query.edit_message_text(_("🏷 *Your Tags*\n\nSelect a tag to see conversations:"), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return SEARCH_INPUT


@restricted
async def tag_browse_results_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show conversations with a specific tag."""
    query = update.callback_query
    await query.answer()
    tag = query.data.split("#")[1]

    pool = _get_pool(context)
    user_id = update.effective_user.id
    results = await get_conversations_by_tag(pool, user_id, tag)

    if not results:
        keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data="Browse_Tags")]]
        await query.edit_message_text(_(f"No conversations with tag '{tag}'."), reply_markup=InlineKeyboardMarkup(keyboard))
        return SEARCH_INPUT

    text = f"🏷 *Conversations tagged \"{tag}\"*\n\n"
    keyboard = []
    for r in results[:10]:
        title = r['title'][:35] + "..." if len(r['title']) > 35 else r['title']
        keyboard.append([InlineKeyboardButton(f"💬 {title}", callback_data=f"CONV_SELECT#{r['conversation_id']}")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back to Tags"), callback_data="Browse_Tags")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


# --- Export Conversation Handler ---

@restricted
async def export_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Export conversation as a text/markdown file."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        await query.edit_message_text(_("Conversation not found."))
        return CONVERSATION_HISTORY

    title = conv.get('title', 'Untitled')
    history = json.loads(conv.get('history', '[]'))

    # Format as markdown
    text = f"# {title}\n\n"
    text += f"Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n"
    for entry in history:
        role = entry.get('role', 'unknown')
        parts = entry.get('parts', [])
        for p in parts:
            if p.get('text'):
                prefix = "**User:**" if role == "user" else "**Assistant:**"
                text += f"{prefix}\n{p['text']}\n\n---\n\n"

    file_buf = io.BytesIO(text.encode('utf-8'))
    safe_title = re.sub(r'[^\w\s-]', '', title)[:30].strip()
    file_name = f"{safe_title or 'conversation'}.md"
    file_buf.name = file_name

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=file_buf,
        filename=file_name,
        caption=f"📤 Exported: {title}"
    )

    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data=f"CONV_SELECT#{conv_id}")]]
    await query.edit_message_text(_("Conversation exported!"), reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


# --- Share Conversation Handler ---

@restricted
async def share_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Share conversation as formatted text."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        return CONVERSATION_HISTORY

    title = conv.get('title', 'Untitled')
    history = json.loads(conv.get('history', '[]'))

    # Format for sharing (compact)
    text = f"💬 *{title}*\n\n"
    msg_count = 0
    for entry in history:
        parts = entry.get('parts', [])
        for p in parts:
            if p.get('text'):
                role = entry.get('role', '')
                emoji = "👤" if role == "user" else "🤖"
                content = p['text'][:200]
                if len(p['text']) > 200:
                    content += "..."
                text += f"{emoji} {content}\n\n"
                msg_count += 1
                if msg_count >= 10:
                    break
        if msg_count >= 10:
            remaining = len(history) - 10
            if remaining > 0:
                text += f"_... and {remaining} more messages_\n"
            break

    text += f"\n_Shared from Gemini Chat Bot_"

    parts_list = split_message(text)
    for part in parts_list:
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=part, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=strip_markdown(part))
    return CONVERSATION_HISTORY


# --- Tag Conversation Handlers ---

@restricted
async def tag_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt user to enter a tag for the current conversation."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    existing_tags = await get_conversation_tags(pool, user_id, conv_id)

    text = "🏷 *Tag this conversation*\n\n"
    if existing_tags:
        text += f"Current tags: {', '.join(existing_tags)}\n\n"
    text += "Type a tag name to add, or tap an existing tag to remove it."

    keyboard = []
    for tag in existing_tags:
        keyboard.append([InlineKeyboardButton(f"❌ Remove: {tag}", callback_data=f"TAG_REMOVE#{tag}")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back"), callback_data=f"CONV_SELECT#{conv_id}")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return TAGS_INPUT


@restricted
async def handle_tag_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a new tag for the current conversation."""
    tag = update.message.text.strip().lower()[:30]
    if not tag:
        await update.message.reply_text(_("Please enter a valid tag name."))
        return TAGS_INPUT

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CHOOSING

    pool = _get_pool(context)
    user_id = update.effective_user.id
    await add_conversation_tag(pool, user_id, conv_id, tag)

    keyboard = [[InlineKeyboardButton(_("🔙 Back to Conversation"), callback_data=f"CONV_SELECT#{conv_id}")]]
    await update.message.reply_text(f"✅ Tag '{tag}' added!", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONVERSATION_HISTORY


@restricted
async def remove_tag_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remove a tag from the current conversation."""
    query = update.callback_query
    await query.answer()
    tag = query.data.split("#")[1]

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    await remove_conversation_tag(pool, user_id, conv_id, tag)

    # Refresh the tag view
    return await tag_conversation_handler(update, context)


# --- Usage Dashboard Handler ---

@restricted
async def usage_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show usage statistics."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    stats = await get_user_stats(pool, user_id)

    text = "📊 *Usage Dashboard*\n"
    text += "━" * 24 + "\n\n"
    text += f"💬 Conversations: {stats['conversations']}\n"
    text += f"📋 Active Tasks: {stats['active_tasks']} / {stats['total_tasks']} total\n"
    text += f"⏰ Reminders: {stats['completed_reminders']} ✅ / {stats['total_reminders']} total\n"
    text += f"📚 Knowledge Docs: {stats['knowledge_docs']}\n"

    if stats.get('member_since'):
        text += f"\n📅 Using since: {str(stats['member_since'])[:10]}"

    # Per-user token usage from database
    token_stats = await get_user_token_stats(pool, user_id)
    if token_stats and token_stats.get("total_tokens"):
        text += f"\n\n🔢 *Token Usage (All Time)*\n"
        text += f"Total: {token_stats['total_tokens']:,} tokens ({token_stats['total_requests']} requests)\n"
        text += f"  Input: {token_stats['prompt_tokens']:,}\n"
        text += f"  Output: {token_stats['completion_tokens']:,}\n"

        # Cached tokens with savings estimate
        if token_stats.get('cached_tokens'):
            cached = token_stats['cached_tokens']
            # Cached tokens cost ~75% less than regular input
            estimated_savings = cached * 0.75
            text += f"  💾 Cached: {cached:,} (saved ~{int(estimated_savings):,} equiv. tokens)\n"

        # Thinking tokens
        if token_stats.get('thinking_tokens'):
            text += f"  💭 Thinking: {token_stats['thinking_tokens']:,}\n"

        text += f"\n📅 *Today*\n"
        text += f"  Tokens: {token_stats.get('today_tokens', 0):,}\n"
        if token_stats.get('today_cached'):
            text += f"  Cached: {token_stats['today_cached']:,}\n"

        text += f"\n📆 *Last 7 Days*\n"
        text += f"  Tokens: {token_stats.get('week_tokens', 0):,}\n"
        if token_stats.get('week_cached'):
            text += f"  Cached: {token_stats['week_cached']:,}\n"

    keyboard = [[InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")]]

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING


# --- Shortcuts Handlers ---

@restricted
async def open_shortcuts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show user shortcuts."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    shortcuts = await get_user_shortcuts(pool, user_id)

    text = "⚡ *Quick Shortcuts*\n\n"
    text += "Create custom commands that auto-send messages to Gemini.\n\n"

    keyboard = [[InlineKeyboardButton(_("➕ Add Shortcut"), callback_data="Add_Shortcut")]]

    if shortcuts:
        for s in shortcuts[:10]:
            text += f"• /{s['command']} → {s['response_text'][:40]}...\n"
            keyboard.append([InlineKeyboardButton(f"❌ Delete /{s['command']}", callback_data=f"SHORTCUT_DELETE#{s['id']}")])
    else:
        text += "No shortcuts yet. Add one to get started!"

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return SHORTCUTS_MENU


@restricted
async def start_add_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt to add a new shortcut."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        _("⚡ *Add Shortcut*\n\n"
          "Enter in format: command | prompt\n\n"
          "Example: summarize | Summarize the following text in 3 bullet points\n\n"
          "Then in conversation, type /summarize to auto-send that prompt."),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Shortcuts_Menu")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return SHORTCUTS_INPUT


@restricted
async def handle_shortcut_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a new shortcut."""
    text = update.message.text
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Shortcuts"), callback_data="Shortcuts_Menu")]])

    try:
        command, response_text = [s.strip() for s in text.split('|', 1)]
        command = command.lower().replace('/', '').replace(' ', '_')[:20]
        if not command or not response_text:
            raise ValueError("Empty command or response")

        pool = _get_pool(context)
        user_id = update.effective_user.id
        await add_shortcut(pool, user_id, command, response_text)

        await update.message.reply_text(f"✅ Shortcut /{command} saved!", reply_markup=back_btn)
    except (ValueError, IndexError):
        await update.message.reply_text(_("Invalid format. Use: command | prompt text"), reply_markup=back_btn)

    return SHORTCUTS_MENU


@restricted
async def delete_shortcut_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete a shortcut."""
    query = update.callback_query
    await query.answer()
    shortcut_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_shortcut(pool, update.effective_user.id, shortcut_id)
    await open_shortcuts_menu(update, context)
    return SHORTCUTS_MENU


# --- Pinned Context Handlers ---

@restricted
async def open_pinned_context_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show pinned context settings."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    user = await get_user(pool, user_id)
    current_context = user.get('pinned_context') if user else None

    text = "📌 *Pinned Context*\n\n"
    text += "This context is included in ALL your conversations automatically.\n"
    text += "Use it for information Gemini should always know about you.\n\n"
    if current_context:
        text += f"Current:\n_{current_context}_\n\n"
        text += "Send new text to update, or tap Clear to remove."
    else:
        text += "No pinned context set. Send text to add one.\n\n"
        text += "Examples:\n• \"I'm a Python developer working on web apps\"\n• \"Always respond in formal English\"\n• \"My timezone is EST\""

    keyboard = []
    if current_context:
        keyboard.append([InlineKeyboardButton(_("🗑 Clear Context"), callback_data="Clear_Pinned_Context")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return PINNED_CONTEXT_INPUT


@restricted
async def handle_pinned_context_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save pinned context."""
    pinned_text = update.message.text.strip()[:500]
    user_id = update.effective_user.id
    pool = _get_pool(context)
    await update_user_settings(pool, user_id, pinned_context=pinned_text)

    # Reset current chat so it picks up new context
    context.user_data["gemini_chat"] = None

    keyboard = [[InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")]]
    await update.message.reply_text(_("✅ Pinned context updated! It will apply to your next conversation."), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


@restricted
async def clear_pinned_context_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Clear pinned context."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    await update_user_settings(pool, user_id, pinned_context="")
    context.user_data["gemini_chat"] = None

    keyboard = [[InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")]]
    await query.edit_message_text(_("📌 Pinned context cleared."), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


# --- Language Handler ---

@restricted
async def language_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show language selection menu."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    user = await get_user(pool, user_id)
    current_lang = user.get('language', 'auto') if user else 'auto'

    languages = [
        ("auto", "🌐 Auto-detect"),
        ("en", "🇬🇧 English"),
        ("fa", "🇮🇷 فارسی"),
        ("es", "🇪🇸 Español"),
        ("fr", "🇫🇷 Français"),
        ("de", "🇩🇪 Deutsch"),
        ("zh", "🇨🇳 中文"),
        ("ja", "🇯🇵 日本語"),
        ("ko", "🇰🇷 한국어"),
        ("ar", "🇸🇦 العربية"),
        ("ru", "🇷🇺 Русский"),
        ("pt", "🇧🇷 Português"),
        ("tr", "🇹🇷 Türkçe"),
    ]

    keyboard = []
    for code, name in languages:
        prefix = "✅ " if code == current_lang else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"SET_LANG_{code}")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back"), callback_data="Settings_Menu")])

    await query.edit_message_text(_("🌍 Select your preferred language:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


@restricted
async def set_language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set user language preference."""
    query = update.callback_query
    await query.answer()

    lang = query.data.replace("SET_LANG_", "")
    user_id = update.effective_user.id
    pool = _get_pool(context)
    await update_user_settings(pool, user_id, language=lang)

    # Reset chat to pick up new language
    context.user_data["gemini_chat"] = None

    await open_settings_menu(update, context)
    return SETTINGS_MENU


# --- Inline Query Handler ---

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline queries (@bot query in any chat)."""
    query_text = update.inline_query.query.strip()
    if not query_text or len(query_text) < 3:
        return

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user = await get_user(pool, user_id)

    api_key = user.get('api_key') if user else None
    if not api_key:
        api_key = GEMINI_API_TOKEN
    if not api_key:
        results = [InlineQueryResultArticle(
            id="no_key",
            title="API Key Required",
            input_message_content=InputTextMessageContent("Please set up your API key first by sending /start to the bot.")
        )]
        await update.inline_query.answer(results, cache_time=0)
        return

    try:
        gemini = GeminiChat(api_key)
        response, _usage = await gemini.one_shot(f"Answer briefly and concisely in 2-3 sentences: {query_text}")

        description = response[:100] if response else "No response"
        results = [InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"Answer: {query_text[:50]}",
            description=description,
            input_message_content=InputTextMessageContent(response)
        )]
        await update.inline_query.answer(results, cache_time=300)
    except Exception as e:
        logger.error(f"Inline query error: {e}")
        results = [InlineQueryResultArticle(
            id="error",
            title="Error processing query",
            input_message_content=InputTextMessageContent(f"Sorry, couldn't process: {query_text}")
        )]
        await update.inline_query.answer(results, cache_time=0)


# --- Weekly Summary Task ---

async def weekly_summary_task():
    """Send weekly digest to users with recent activity."""
    if not _application:
        return

    pool = _application.bot_data.get("db_pool")
    if not pool:
        return

    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        rows = await pool.execute_fetch_all(
            "SELECT DISTINCT user_id FROM conversations WHERE created_at >= ?",
            (week_ago,),
        )
    except Exception as e:
        logger.error(f"Failed to get active users for weekly summary: {e}")
        return

    for row in rows:
        user_id = row["user_id"]
        try:
            stats = await get_user_stats(pool, user_id)

            summary = "📊 *Weekly Summary*\n"
            summary += "━" * 24 + "\n\n"
            summary += f"💬 Conversations: {stats['conversations']}\n"
            summary += f"📋 Active Tasks: {stats['active_tasks']}\n"
            summary += f"⏰ Reminders completed: {stats['completed_reminders']}/{stats['total_reminders']}\n"
            summary += f"📚 Knowledge Docs: {stats['knowledge_docs']}\n"
            summary += f"\nKeep up the great work! 🎯"

            await _application.bot.send_message(chat_id=user_id, text=summary, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Failed to send weekly summary to {user_id}: {e}")


# --- Templates Handlers ---

@restricted
async def templates_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show conversation templates."""
    query = update.callback_query
    await query.answer()

    text = "📝 *Conversation Templates*\n\nStart a conversation with a specialized persona:\n"

    keyboard = []
    for t in CONVERSATION_TEMPLATES:
        keyboard.append([InlineKeyboardButton(t['name'], callback_data=f"TEMPLATE#{t['id']}")])
    keyboard.append([InlineKeyboardButton(_("🌐 Translation Mode"), callback_data="Translation_Mode")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return TEMPLATES_MENU


@restricted
async def select_template_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start conversation with a template persona."""
    query = update.callback_query
    await query.answer()

    template_id = query.data.split("#")[1]
    template = next((t for t in CONVERSATION_TEMPLATES if t['id'] == template_id), None)
    if not template:
        return CHOOSING

    # Set the template persona as system instruction for this session
    context.user_data["gemini_chat"] = None
    context.user_data["conversation_id"] = None
    context.user_data["template_persona"] = template['persona']

    # Create chat with template persona
    user_id = update.effective_user.id
    pool = _get_pool(context)
    user_data = await get_user(pool, user_id)
    api_key = context.user_data.get("api_key") or (user_data.get('api_key') if user_data else None) or GEMINI_API_TOKEN
    model_name = user_data.get('model_name') if user_data else context.user_data.get("model_name")
    pinned_context = user_data.get('pinned_context') if user_data else None
    user_language = user_data.get('language', 'auto') if user_data else 'auto'

    tools = []
    if context.user_data.get("web_search"):
        tools.append("google_search")

    gemini_chat = GeminiChat(
        api_key, model_name=model_name, tools=tools,
        system_instruction=template['persona'],
        pinned_context=pinned_context, language=user_language,
    )
    await gemini_chat.start_chat()
    context.user_data["gemini_chat"] = gemini_chat

    await query.edit_message_text(
        f"{template['name']} mode activated!\n\nSend your first message.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again")]])
    )
    return CONVERSATION


# --- Translation Mode Handler ---

@restricted
async def translation_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start translation mode - select target language."""
    query = update.callback_query
    await query.answer()

    languages = [
        ("en", "🇬🇧 English"), ("fa", "🇮🇷 فارسی"), ("es", "🇪🇸 Español"),
        ("fr", "🇫🇷 Français"), ("de", "🇩🇪 Deutsch"), ("zh", "🇨🇳 中文"),
        ("ja", "🇯🇵 日本語"), ("ko", "🇰🇷 한국어"), ("ar", "🇸🇦 العربية"),
        ("ru", "🇷🇺 Русский"), ("pt", "🇧🇷 Português"), ("tr", "🇹🇷 Türkçe"),
    ]

    keyboard = []
    for code, name in languages:
        keyboard.append([InlineKeyboardButton(name, callback_data=f"TRANSLATE_TO#{code}")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back"), callback_data="Templates_Menu")])

    await query.edit_message_text(
        _("🌐 *Translation Mode*\n\nSelect the target language. Every message you send will be translated."),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return TEMPLATES_MENU


@restricted
async def start_translation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start translation conversation with selected language."""
    query = update.callback_query
    await query.answer()

    target_lang = query.data.split("#")[1]
    lang_names = {"en": "English", "fa": "Persian", "es": "Spanish", "fr": "French", "de": "German",
                  "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                  "ru": "Russian", "pt": "Portuguese", "tr": "Turkish"}
    lang_name = lang_names.get(target_lang, target_lang)

    persona = (
        f"You are a translator. Translate every message the user sends into {lang_name}. "
        "Output ONLY the translation, nothing else. No explanations, no notes. "
        "If the input is already in the target language, translate it to English instead."
    )

    context.user_data["gemini_chat"] = None
    context.user_data["conversation_id"] = None

    user_id = update.effective_user.id
    pool = _get_pool(context)
    user_data = await get_user(pool, user_id)
    api_key = context.user_data.get("api_key") or (user_data.get('api_key') if user_data else None) or GEMINI_API_TOKEN

    gemini_chat = GeminiChat(api_key, system_instruction=persona)
    await gemini_chat.start_chat()
    context.user_data["gemini_chat"] = gemini_chat

    await query.edit_message_text(
        f"🌐 Translation mode → *{lang_name}*\n\nSend any text to translate.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return CONVERSATION


# --- Bookmarks Handlers ---

@restricted
async def bookmarks_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show user's bookmarks."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    bookmarks = await get_user_bookmarks(pool, user_id)

    text = "⭐ *Your Bookmarks*\n\n"
    keyboard = []

    if bookmarks:
        for b in bookmarks[:15]:
            preview = b['message_text'][:80].replace('\n', ' ')
            if len(b['message_text']) > 80:
                preview += "..."
            text += f"• {preview}\n\n"
            keyboard.append([InlineKeyboardButton(f"❌ Delete #{b['id']}", callback_data=f"BOOKMARK_DELETE#{b['id']}")])
    else:
        text += "No bookmarks yet. Use the ⭐ button on AI responses to bookmark them."

    keyboard.append([InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return BOOKMARKS_MENU


@restricted
async def delete_bookmark_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete a bookmark."""
    query = update.callback_query
    await query.answer()
    bookmark_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_bookmark(pool, update.effective_user.id, bookmark_id)
    return await bookmarks_menu_handler(update, context)


@restricted
async def bookmark_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bookmark the current AI response."""
    query = update.callback_query
    await query.answer("⭐ Bookmarked!", show_alert=False)

    # Remove buttons from this AI response
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    message_text = query.message.text or ""
    if not message_text:
        return CONVERSATION

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv_id = context.user_data.get("conversation_id")
    await add_bookmark(pool, user_id, message_text[:500], conv_id)
    return CONVERSATION


# --- Prompt Library Handlers ---

@restricted
async def prompt_library_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show prompt library."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    prompts = await get_user_prompts(pool, user_id)

    text = "📖 *Prompt Library*\n\nSave and reuse your favorite prompts.\n\n"
    keyboard = [[InlineKeyboardButton(_("➕ Add Prompt"), callback_data="Add_Prompt")]]

    if prompts:
        current_cat = None
        for p in prompts[:15]:
            if p['category'] != current_cat:
                current_cat = p['category']
                text += f"\n*{current_cat.title()}:*\n"
            text += f"• {p['title']}\n"
            keyboard.append([
                InlineKeyboardButton(f"▶️ {p['title'][:25]}", callback_data=f"USE_PROMPT#{p['id']}"),
                InlineKeyboardButton("❌", callback_data=f"PROMPT_DELETE#{p['id']}"),
            ])
    else:
        text += "No saved prompts yet."

    keyboard.append([InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return PROMPT_LIBRARY


@restricted
async def start_add_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt to add a new prompt to library."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        _("📖 *Add Prompt*\n\nEnter in format: title | prompt text\n\nOptionally add category: title | prompt text | category\n\nExample: Summarize | Please summarize the following text concisely | writing"),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="Prompt_Library")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return PROMPT_ADD


@restricted
async def handle_prompt_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a new prompt."""
    text = update.message.text
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Prompts"), callback_data="Prompt_Library")]])

    try:
        parts = [s.strip() for s in text.split('|')]
        title = parts[0]
        prompt_text = parts[1] if len(parts) > 1 else title
        category = parts[2] if len(parts) > 2 else 'general'

        if not title:
            raise ValueError("Empty title")

        pool = _get_pool(context)
        user_id = update.effective_user.id
        await add_prompt(pool, user_id, title[:50], prompt_text, category[:20])

        await update.message.reply_text(f"✅ Prompt '{title}' saved!", reply_markup=back_btn)
    except (ValueError, IndexError):
        await update.message.reply_text(_("Invalid format. Use: title | prompt text"), reply_markup=back_btn)

    return PROMPT_LIBRARY


@restricted
async def use_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Use a saved prompt - start conversation with it."""
    query = update.callback_query
    await query.answer()
    prompt_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    user_id = update.effective_user.id
    prompts = await get_user_prompts(pool, user_id)
    prompt = next((p for p in prompts if p['id'] == prompt_id), None)

    if not prompt:
        return PROMPT_LIBRARY

    # Store the prompt text to be sent as the first message
    context.user_data["pending_prompt"] = prompt['prompt_text']
    context.user_data["gemini_chat"] = None
    context.user_data["conversation_id"] = None

    await query.edit_message_text(
        f"📖 Prompt loaded: *{prompt['title']}*\n\n_{prompt['prompt_text'][:100]}_\n\nSend your content or type to start.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return CONVERSATION


@restricted
async def delete_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete a prompt."""
    query = update.callback_query
    await query.answer()
    prompt_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_prompt(pool, update.effective_user.id, prompt_id)
    return await prompt_library_handler(update, context)


# --- Follow-up Suggestion Handler ---

@restricted
async def suggest_followup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Suggest follow-up questions based on the conversation."""
    query = update.callback_query
    await query.answer()

    # Remove buttons from this AI response
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    gemini_chat = context.user_data.get("gemini_chat")
    if not gemini_chat:
        return CONVERSATION

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response, _usage, _sources = await gemini_chat.send_message(
            "Based on our conversation, suggest exactly 3 brief follow-up questions I could ask. "
            "Format: numbered list, one line each. Keep them short and specific."
        )

        keyboard = [
            [
                InlineKeyboardButton(_("💾 Save & Menu"), callback_data="Start_Again_SAVE_CONV"),
                InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again_CONV"),
            ],
        ]

        try:
            await query.message.reply_text(
                f"💡 *Follow-up suggestions:*\n\n{response}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest:
            await query.message.reply_text(
                f"Follow-up suggestions:\n\n{response}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    except Exception as e:
        logger.error(f"Follow-up suggestion failed: {e}")

    return CONVERSATION


# --- Voice Output Handler ---

@restricted
async def voice_output_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Convert last AI response to voice."""
    query = update.callback_query
    await query.answer("🔊 Generating audio...")

    # Remove buttons from this AI response
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    last_response = context.user_data.get("last_ai_response", "")
    if not last_response:
        return CONVERSATION

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="record_voice")

    try:
        # Determine language
        pool = _get_pool(context)
        user = await get_user(pool, update.effective_user.id)
        lang = user.get('language', 'en') if user else 'en'
        if lang == 'auto':
            lang = 'en'

        # Strip markdown for cleaner TTS
        clean_text = strip_markdown(last_response)[:5000]

        loop = asyncio.get_event_loop()

        def _generate_tts():
            from gtts import gTTS
            tts = gTTS(text=clean_text, lang=lang)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            return buf

        audio_buf = await loop.run_in_executor(None, _generate_tts)
        audio_buf.name = "response.mp3"

        await context.bot.send_audio(
            chat_id=update.effective_chat.id,
            audio=audio_buf,
            title="AI Response",
        )
    except Exception as e:
        logger.error(f"TTS generation failed: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=_("❌ Voice generation failed. Make sure the language is supported.")
        )

    return CONVERSATION


# --- Feedback Handlers ---

@restricted
async def feedback_up_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Record positive feedback."""
    query = update.callback_query
    await query.answer("👍 Thanks for the feedback!", show_alert=False)

    # Remove buttons from this AI response
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    pool = _get_pool(context)
    message_preview = (query.message.text or "")[:100]
    await add_feedback(pool, update.effective_user.id, message_preview, 1)
    return CONVERSATION


@restricted
async def feedback_down_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Record negative feedback."""
    query = update.callback_query
    await query.answer("👎 Noted, we'll try to improve!", show_alert=False)

    # Remove buttons from this AI response
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    pool = _get_pool(context)
    message_preview = (query.message.text or "")[:100]
    await add_feedback(pool, update.effective_user.id, message_preview, -1)
    return CONVERSATION


# --- Conversation Branching Handler ---

@restricted
async def branch_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create a branch (copy) of the current conversation."""
    query = update.callback_query
    await query.answer()

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        await query.edit_message_text(_("No conversation selected."))
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        return CONVERSATION_HISTORY

    new_conv_id = f"conv{uuid.uuid4().hex[:6]}"
    title = conv.get('title', 'Untitled')
    history = conv.get('history', '[]')

    await create_conversation_branch(pool, user_id, conv_id, new_conv_id, title, history)

    context.user_data["conversation_id"] = new_conv_id
    context.user_data["gemini_chat"] = None

    keyboard = [
        [InlineKeyboardButton(_("▶️ Continue Branch"), callback_data="New_Conversation")],
        [InlineKeyboardButton(_("🔙 Back to menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(
        f"🔀 Branch created: *[Branch] {title}*\n\nYou can now continue this conversation independently.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return CONVERSATION_HISTORY


# --- Resume Point Handler ---

@restricted
async def set_resume_point_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mark current position as resume point."""
    query = update.callback_query
    await query.answer("📍 Resume point set!", show_alert=False)

    conv_id = context.user_data.get("conversation_id")
    if not conv_id:
        return CONVERSATION_HISTORY

    pool = _get_pool(context)
    user_id = update.effective_user.id
    conv = await select_conversation_by_id(pool, (user_id, conv_id))
    if not conv:
        return CONVERSATION_HISTORY

    history = json.loads(conv.get('history', '[]'))
    resume_idx = len(history)

    await update_conversation_resume(pool, user_id, conv_id, resume_idx)

    keyboard = [[InlineKeyboardButton(_("🔙 Back"), callback_data=f"CONV_SELECT#{conv_id}")]]
    await query.edit_message_text(
        f"📍 Resume point set at message {resume_idx}.\n\nWhen you continue this conversation, you'll see where you left off.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONVERSATION_HISTORY


# --- Daily Briefing Handlers ---

@restricted
async def briefing_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Configure daily briefing."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    user = await get_user(pool, user_id)
    current_time = user.get('briefing_time') if user else None

    text = "🔔 *Daily Briefing*\n\n"
    text += "Get a daily summary with your tasks, reminders, and a motivational message.\n\n"
    if current_time:
        text += f"Currently scheduled at: *{current_time} UTC*\n\n"
        text += "Send a new time (HH:MM) to change, or tap Disable."
    else:
        text += "Not active. Send a time (HH:MM) in UTC to enable."

    keyboard = []
    if current_time:
        keyboard.append([InlineKeyboardButton(_("❌ Disable Briefing"), callback_data="Disable_Briefing")])
    keyboard.append([InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return BRIEFING_MENU


@restricted
async def handle_briefing_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Set daily briefing time."""
    time_str = update.message.text.strip()
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to Settings"), callback_data="Settings_Menu")]])

    try:
        datetime.strptime(time_str, "%H:%M")
        pool = _get_pool(context)
        user_id = update.effective_user.id
        await update_user_settings(pool, user_id, briefing_time=time_str)

        await update.message.reply_text(f"✅ Daily briefing set for {time_str} UTC!", reply_markup=back_btn)
        return SETTINGS_MENU
    except ValueError:
        await update.message.reply_text(_("Invalid format. Use HH:MM (e.g., 08:00)"), reply_markup=back_btn)
        return BRIEFING_MENU


@restricted
async def disable_briefing_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Disable daily briefing."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    await update_user_settings(pool, update.effective_user.id, briefing_time="")

    keyboard = [[InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")]]
    await query.edit_message_text(_("🔔 Daily briefing disabled."), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU


# --- URL Monitor Handlers ---

@restricted
async def url_monitor_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show URL monitors."""
    query = update.callback_query
    await query.answer()

    pool = _get_pool(context)
    user_id = update.effective_user.id
    monitors = await get_user_monitors(pool, user_id)

    text = "🔗 *URL Monitor*\n\n"
    text += "Watch web pages for changes and get notified.\n\n"

    keyboard = [[InlineKeyboardButton(_("➕ Add URL Monitor"), callback_data="Add_URL_Monitor")]]

    if monitors:
        for m in monitors[:10]:
            url_short = m['url'][:40] + "..." if len(m['url']) > 40 else m['url']
            status = "✅" if m['status'] == 'active' else "⏸"
            text += f"{status} {url_short} (every {m['check_interval_hours']}h)\n"
            keyboard.append([InlineKeyboardButton(f"❌ Delete: {url_short[:25]}", callback_data=f"MONITOR_DELETE#{m['id']}")])
    else:
        text += "No monitors set up."

    keyboard.append([InlineKeyboardButton(_("🔙 Back to Settings"), callback_data="Settings_Menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await query.edit_message_text(strip_markdown(text), reply_markup=InlineKeyboardMarkup(keyboard))
    return URL_MONITOR_MENU


@restricted
async def start_add_url_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt to add URL monitor."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        _("🔗 *Add URL Monitor*\n\nEnter a URL to monitor:\n\nOptionally add check interval: URL | hours\nExample: https://example.com | 6\n\nDefault interval: 1 hour"),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Back"), callback_data="URL_Monitor_Menu")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return URL_MONITOR_INPUT


@restricted
async def handle_url_monitor_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save a new URL monitor."""
    text = update.message.text.strip()
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="URL_Monitor_Menu")]])

    try:
        parts = [s.strip() for s in text.split('|')]
        url = parts[0]
        interval = int(parts[1]) if len(parts) > 1 else 1

        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        # Validate URL by trying to fetch it
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.head(url)
            if resp.status_code >= 400:
                await update.message.reply_text(_("URL returned an error. Please check and try again."), reply_markup=back_btn)
                return URL_MONITOR_INPUT

        pool = _get_pool(context)
        user_id = update.effective_user.id
        await add_url_monitor(pool, user_id, url, max(1, min(interval, 168)))

        await update.message.reply_text(f"✅ URL monitor added!\n🔗 {url}\n⏰ Check every {interval}h", reply_markup=back_btn)
    except Exception as e:
        logger.warning(f"URL monitor add failed: {e}")
        await update.message.reply_text(_("Invalid input. Please enter a valid URL."), reply_markup=back_btn)

    return URL_MONITOR_MENU


@restricted
async def delete_url_monitor_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Delete a URL monitor."""
    query = update.callback_query
    await query.answer()
    monitor_id = int(query.data.split("#")[1])

    pool = _get_pool(context)
    await delete_url_monitor(pool, update.effective_user.id, monitor_id)
    return await url_monitor_menu_handler(update, context)


# --- Background Tasks ---

async def check_url_monitors_task():
    """Background task to check URL monitors for changes."""
    if not _application:
        return

    pool = _application.bot_data.get("db_pool")
    if not pool:
        return

    monitors = await get_active_monitors(pool)
    now = datetime.now()

    for m in monitors:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(m['url'], headers={"User-Agent": "Mozilla/5.0"})
                content_hash = hashlib.md5(resp.text.encode()).hexdigest()

                if m['last_hash'] and m['last_hash'] != content_hash:
                    await _application.bot.send_message(
                        chat_id=m['user_id'],
                        text=f"🔗 *URL Change Detected!*\n\n{m['url']}\n\nThe page content has changed since your last check.",
                        parse_mode=ParseMode.MARKDOWN
                    )

                await update_monitor_hash(pool, m['id'], content_hash)
        except Exception as e:
            logger.warning(f"URL monitor check failed for {m['url']}: {e}")


async def daily_briefing_task():
    """Background task to send daily briefings."""
    if not _application:
        return

    pool = _application.bot_data.get("db_pool")
    if not pool:
        return

    current_time = datetime.now().strftime("%H:%M")

    try:
        rows = await pool.execute_fetch_all(
            "SELECT user_id, briefing_time FROM users WHERE briefing_time=?",
            (current_time,),
        )
    except Exception as e:
        logger.error(f"Failed to get briefing users: {e}")
        return

    for row in rows:
        user_id = row["user_id"]
        try:
            stats = await get_user_stats(pool, user_id)

            # Get today's tasks and pending reminders
            from database.database import get_user_tasks, get_user_reminders
            tasks = await get_user_tasks(pool, user_id)
            reminders = await get_user_reminders(pool, user_id)
            pending_reminders = [r for r in reminders if r['status'] == 'pending']

            briefing = "☀️ *Good morning! Here's your daily briefing:*\n"
            briefing += "━" * 24 + "\n\n"

            if tasks:
                active = [t for t in tasks if t.get('status') == 'active']
                if active:
                    briefing += f"📋 *Active Tasks:* {len(active)}\n"
                    for t in active[:3]:
                        briefing += f"  • {t['prompt'][:40]}\n"
                    briefing += "\n"

            if pending_reminders:
                briefing += f"⏰ *Pending Reminders:* {len(pending_reminders)}\n"
                for r in pending_reminders[:3]:
                    briefing += f"  • {r['remind_at']}: {r['reminder_text'][:30]}\n"
                briefing += "\n"

            briefing += f"💬 Total conversations: {stats['conversations']}\n"
            briefing += f"\n✨ Have a productive day!"

            await _application.bot.send_message(chat_id=user_id, text=briefing, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Failed to send briefing to {user_id}: {e}")
