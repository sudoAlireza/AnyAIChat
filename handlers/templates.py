"""Conversation template and translation mode handlers."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool, get_api_key, _safe_callback_data
from handlers.states import CHOOSING, CONVERSATION, TEMPLATES_MENU
from chat.session import ChatSession
from database.database import get_user
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)

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

    template_id = _safe_callback_data(query.data)
    if template_id is None:
        return CHOOSING
    template = next((t for t in CONVERSATION_TEMPLATES if t['id'] == template_id), None)
    if not template:
        return CHOOSING

    # Set the template persona as system instruction for this session
    context.user_data["chat_session"] = None
    context.user_data["conversation_id"] = None
    context.user_data["template_persona"] = template['persona']

    # Create chat with template persona
    user_id = update.effective_user.id
    pool = _get_pool(context)
    user_data = await get_user(pool, user_id)
    api_key = await get_api_key(context, user_id)
    model_name = user_data.get('model_name') if user_data else context.user_data.get("model_name")
    pinned_context = user_data.get('pinned_context') if user_data else None
    user_language = user_data.get('language', 'auto') if user_data else 'auto'

    provider_name = context.user_data.get("active_provider", "gemini")
    chat = ChatSession(
        provider_name=provider_name, api_key=api_key, model_name=model_name,
        system_instruction=template['persona'],
        pinned_context=pinned_context, language=user_language,
        web_search=bool(context.user_data.get("web_search")),
    )
    await chat.start_chat()
    context.user_data["chat_session"] = chat

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

    target_lang = _safe_callback_data(query.data)
    if target_lang is None:
        return TEMPLATES_MENU
    lang_names = {"en": "English", "fa": "Persian", "es": "Spanish", "fr": "French", "de": "German",
                  "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                  "ru": "Russian", "pt": "Portuguese", "tr": "Turkish"}
    lang_name = lang_names.get(target_lang, target_lang)

    persona = (
        f"You are a translator. Translate every message the user sends into {lang_name}. "
        "Output ONLY the translation, nothing else. No explanations, no notes. "
        "If the input is already in the target language, translate it to English instead."
    )

    context.user_data["chat_session"] = None
    context.user_data["conversation_id"] = None

    user_id = update.effective_user.id
    pool = _get_pool(context)
    await get_user(pool, user_id)
    api_key = await get_api_key(context, user_id)

    provider_name = context.user_data.get("active_provider", "gemini")
    chat = ChatSession(provider_name=provider_name, api_key=api_key, model_name=None, system_instruction=persona)
    await chat.start_chat()
    context.user_data["chat_session"] = chat

    await query.edit_message_text(
        f"🌐 Translation mode → *{lang_name}*\n\nSend any text to translate.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again")]]),
        parse_mode=ParseMode.MARKDOWN
    )
    return CONVERSATION
