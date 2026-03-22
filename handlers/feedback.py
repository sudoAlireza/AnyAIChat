"""Feedback handlers for thumbs-up / thumbs-down on AI responses."""

import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from handlers.common import restricted, _get_pool
from handlers.states import CONVERSATION
from database.database import add_feedback

logger = logging.getLogger(__name__)


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
