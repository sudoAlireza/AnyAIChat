"""Onboarding handlers: start, API key input, start_over, done.

Refactored from bot/conversation_handlers.py to use the ChatSession abstraction.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from handlers.common import restricted, _, _get_pool, _clear_last_ai_buttons, _current_user_id
from handlers.states import (
    CHOOSING,
    CONVERSATION,
    API_KEY_INPUT,
)
from chat.session import ChatSession
from config import GEMINI_API_TOKEN
from database.database import (
    get_user,
    update_user_api_key,
    create_conversation,
    get_task_by_id,
    add_conversation_tag,
)
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation with /start command and show the main menu."""
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

    if not user or not user.get("api_key"):
        # New user — ask for API key (silently default to gemini provider)
        await update.message.reply_text(_(
            "Welcome! To use this bot, you need to provide your own API Key.\n\n"
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
    context.user_data["api_key"] = user["api_key"]
    context.user_data["model_name"] = user["model_name"]
    context.user_data["web_search"] = bool(user["grounding"])
    context.user_data["system_instruction"] = user.get("system_instruction")
    context.user_data["active_provider"] = user.get("active_provider", "gemini")

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
                    provider_name = context.user_data.get("active_provider", "gemini")
                    model_name = context.user_data.get("model_name")
                    web_search = context.user_data.get("web_search", False)
                    chat = ChatSession(
                        provider_name=provider_name,
                        api_key=api_key,
                        model_name=model_name,
                        system_instruction=sys_instr,
                        web_search=web_search,
                    )
                    await chat.start_chat()
                    context.user_data["chat_session"] = chat
                    context.user_data["conversation_id"] = None

                    keyboard = [[InlineKeyboardButton(_("\U0001f519 Menu"), callback_data="Start_Again")]]
                    await update.message.reply_text(
                        f"\U0001f4ac Let's discuss *Day {dl_day_num}: {day_item['title']}*\n\nSend your question.",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return CONVERSATION
        except (ValueError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Failed to handle discuss deep-link: {e}")

    keyboard = [
        [
            InlineKeyboardButton(_("\U0001f4ac New Chat"), callback_data="New_Conversation"),
            InlineKeyboardButton(_("\U0001f4dd Templates"), callback_data="Templates_Menu"),
        ],
        [
            InlineKeyboardButton(_("\U0001f4c2 History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("\U0001f4cb Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("\u23f0 Reminders"), callback_data="Reminders_Menu"),
            InlineKeyboardButton(_("\U0001f4da Knowledge"), callback_data="Knowledge_Menu"),
        ],
        [
            InlineKeyboardButton(_("\U0001f50d Search"), callback_data="Search_Menu"),
            InlineKeyboardButton(_("\u2b50 Bookmarks"), callback_data="Bookmarks_Menu"),
        ],
        [
            InlineKeyboardButton(_("\U0001f4d6 Prompts"), callback_data="Prompt_Library"),
            InlineKeyboardButton(_("\U0001f4ca Usage"), callback_data="Usage_Dashboard"),
        ],
        [InlineKeyboardButton(_("\u2699\ufe0f Settings"), callback_data="Settings_Menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = _("\u2728 *AI Chat Bot*\n\nAsk me anything \u2014 text, voice, photos, or documents.")

    if update.message:
        try:
            await update.message.reply_text(
                text=welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN,
            )
        except BadRequest:
            await update.message.reply_text(
                text=strip_markdown(welcome_text), reply_markup=reply_markup,
            )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=welcome_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN,
            )
        except BadRequest:
            await update.callback_query.edit_message_text(
                text=strip_markdown(welcome_text), reply_markup=reply_markup,
            )

    return CHOOSING


# ---------------------------------------------------------------------------
# API key input
# ---------------------------------------------------------------------------

@restricted
async def handle_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the user's API key after provider-specific validation."""
    api_key = update.message.text.strip()
    user_id = update.effective_user.id

    # Strip control characters
    api_key = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", api_key)

    provider_name = context.user_data.get("active_provider", "gemini")

    # Provider-specific key format validation
    if provider_name == "gemini":
        if not re.match(r"^AIza[A-Za-z0-9_-]{35,}$", api_key):
            await update.message.reply_text(_(
                "Invalid API key format. Gemini API keys start with 'AIza'. Please try again:"
            ))
            return API_KEY_INPUT
    elif provider_name == "openai":
        if not api_key.startswith("sk-"):
            await update.message.reply_text(_(
                "Invalid API key format. OpenAI keys start with 'sk-'. Please try again:"
            ))
            return API_KEY_INPUT
    elif provider_name == "anthropic":
        if not api_key.startswith("sk-ant-"):
            await update.message.reply_text(_(
                "Invalid API key format. Anthropic keys start with 'sk-ant-'. Please try again:"
            ))
            return API_KEY_INPUT

    # Validate using the provider
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    from providers.registry import ProviderRegistry

    provider = ProviderRegistry().get(provider_name)
    if not provider:
        await update.message.reply_text(_("Provider not available. Please try again."))
        return API_KEY_INPUT

    try:
        valid = await provider.validate_key(api_key)
        if not valid:
            await update.message.reply_text(_(
                "API key validation failed. Please check and try again:"
            ))
            return API_KEY_INPUT
    except Exception as e:
        logger.warning(f"API key validation error: {e}")
        await update.message.reply_text(_(
            "Could not validate API key. Please check and try again:"
        ))
        return API_KEY_INPUT

    pool = _get_pool(context)
    await update_user_api_key(pool, user_id, api_key)

    await update.message.reply_text(_(
        "API Key saved successfully! Now you can start using the bot."
    ))
    return await start(update, context)


# ---------------------------------------------------------------------------
# start_over (close conversation, optionally save, return to menu)
# ---------------------------------------------------------------------------

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
    chat_session: ChatSession | None = context.user_data.get("chat_session")
    if chat_session and query and "_SAVE" in query.data:
        history = chat_session.get_history_as_dicts()
        title = await chat_session.get_chat_title()
        conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"

        await create_conversation(pool, (conv_id, user_id, title, json.dumps(history)))
        logger.info(f"Conversation {conv_id} saved for user {user_id}")

        # Auto-tagging based on title keywords
        try:
            auto_tags: list[str] = []
            title_lower = title.lower()
            tag_keywords = {
                "code": ["code", "programming", "debug", "function", "api", "bug", "error"],
                "writing": ["write", "essay", "article", "story", "blog", "email"],
                "math": ["math", "calculate", "equation", "formula", "number"],
                "science": ["science", "physics", "chemistry", "biology", "research"],
                "language": ["translate", "grammar", "language", "english", "spanish"],
                "work": ["meeting", "project", "deadline", "report", "business"],
                "learning": ["learn", "study", "explain", "tutorial", "course"],
                "creative": ["creative", "idea", "brainstorm", "design", "art"],
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
            text=_("Conversation saved successfully!"),
        )

    # Clean up context
    context.user_data["chat_session"] = None
    context.user_data["conversation_id"] = None

    # Refresh user data from DB
    user = await get_user(pool, user_id)
    if user:
        context.user_data["api_key"] = user["api_key"]
        context.user_data["model_name"] = user["model_name"]
        context.user_data["web_search"] = bool(user["grounding"])
        context.user_data["system_instruction"] = user.get("system_instruction")
        context.user_data["active_provider"] = user.get("active_provider", "gemini")

    # AI response buttons (_CONV): always send menu as new message to preserve the response.
    # Menu buttons: edit the current menu message as normal navigation.
    if from_conversation:
        return await start_menu_new_message(update, context)

    return await start(update, context)


# ---------------------------------------------------------------------------
# start_menu_new_message (send main menu as a separate message)
# ---------------------------------------------------------------------------

async def start_menu_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send main menu as a new message (preserves the AI response above)."""
    keyboard = [
        [
            InlineKeyboardButton(_("\U0001f4ac New Chat"), callback_data="New_Conversation"),
            InlineKeyboardButton(_("\U0001f4dd Templates"), callback_data="Templates_Menu"),
        ],
        [
            InlineKeyboardButton(_("\U0001f4c2 History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("\U0001f4cb Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("\u23f0 Reminders"), callback_data="Reminders_Menu"),
            InlineKeyboardButton(_("\U0001f4da Knowledge"), callback_data="Knowledge_Menu"),
        ],
        [
            InlineKeyboardButton(_("\U0001f50d Search"), callback_data="Search_Menu"),
            InlineKeyboardButton(_("\u2b50 Bookmarks"), callback_data="Bookmarks_Menu"),
        ],
        [
            InlineKeyboardButton(_("\U0001f4d6 Prompts"), callback_data="Prompt_Library"),
            InlineKeyboardButton(_("\U0001f4ca Usage"), callback_data="Usage_Dashboard"),
        ],
        [InlineKeyboardButton(_("\u2699\ufe0f Settings"), callback_data="Settings_Menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = _("\u2728 *AI Chat Bot*\n\nAsk me anything \u2014 text, voice, photos, or documents.")

    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=welcome_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
    except BadRequest:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=strip_markdown(welcome_text),
            reply_markup=reply_markup,
        )
    return CHOOSING


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback handler — delegates to start_over."""
    return await start_over(update, context)
