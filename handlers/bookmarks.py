"""Bookmark handlers."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool
from handlers.states import CONVERSATION, BOOKMARKS_MENU
from database.database import add_bookmark, get_user_bookmarks, delete_bookmark
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)


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
