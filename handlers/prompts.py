"""Prompt library handlers."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool
from handlers.states import CONVERSATION, PROMPT_LIBRARY, PROMPT_ADD
from database.database import add_prompt, get_user_prompts, delete_prompt
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)


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
    context.user_data["chat_session"] = None
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
