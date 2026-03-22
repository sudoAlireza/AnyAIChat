"""Media-related handlers: image generation, follow-up suggestions, voice output."""

import io
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool, get_api_key
from handlers.states import CONVERSATION
from chat.session import ChatSession
from database.database import get_user
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)


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
        api_key = await get_api_key(context, update.effective_user.id)
        provider_name = context.user_data.get("active_provider", "gemini")
        chat = ChatSession(provider_name=provider_name, api_key=api_key, model_name=None)
        await chat.start_chat()
        await chat.generate_image(prompt)

        await msg.edit_text(_("🎨 Image generation requested for: ") + prompt + _("\n\n(Note: Imagen API integration is experimental and may require specific account permissions)"))
    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        await msg.edit_text(_("❌ Failed to generate image. ") + str(e))

    return CONVERSATION


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

    chat_session = context.user_data.get("chat_session")
    if not chat_session:
        return CONVERSATION

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        # Use the send_message method on the session
        response = await chat_session.send_message(
            "Based on our conversation, suggest exactly 3 brief follow-up questions I could ask. "
            "Format: numbered list, one line each. Keep them short and specific."
        )
        # response is a ChatResponse
        response_text = response.text

        keyboard = [
            [
                InlineKeyboardButton(_("💾 Save & Menu"), callback_data="Start_Again_SAVE_CONV"),
                InlineKeyboardButton(_("🔙 Menu"), callback_data="Start_Again_CONV"),
            ],
        ]

        try:
            await query.message.reply_text(
                f"💡 *Follow-up suggestions:*\n\n{response_text}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except BadRequest:
            await query.message.reply_text(
                f"Follow-up suggestions:\n\n{response_text}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    except Exception as e:
        logger.error(f"Follow-up suggestion failed: {e}")

    return CONVERSATION


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
