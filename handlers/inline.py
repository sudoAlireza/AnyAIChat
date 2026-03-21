"""Inline query handler for @bot queries in any chat."""

import uuid
import logging
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import ContextTypes

from handlers.common import _get_pool, get_api_key
from handlers.states import *
from config import GEMINI_MODEL
from chat.session import ChatSession
from database.database import get_user

logger = logging.getLogger(__name__)


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
        results = [InlineQueryResultArticle(
            id="no_key",
            title="API Key Required",
            input_message_content=InputTextMessageContent("Please set up your API key first by sending /start to the bot.")
        )]
        await update.inline_query.answer(results, cache_time=0)
        return

    try:
        provider_name = user.get('active_provider', 'gemini') if user else 'gemini'
        chat = ChatSession(provider_name=provider_name, api_key=api_key, model_name=None)
        await chat.start_chat()
        response = await chat.one_shot(f"Answer briefly in 2-3 sentences: {query_text}")
        response_text = response.text

        description = response_text[:100] if response_text else "No response"
        results = [InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title=f"Answer: {query_text[:50]}",
            description=description,
            input_message_content=InputTextMessageContent(response_text)
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
