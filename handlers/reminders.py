"""Reminder management handlers — extracted from bot/conversation_handlers.py."""

import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.common import restricted, _, _get_pool, get_api_key, _safe_callback_data
from handlers.states import REMINDERS_MENU, REMINDERS_INPUT
from chat.session import ChatSession
from database.database import (
    add_reminder, get_user_reminders, delete_reminder,
    get_pending_reminders, update_reminder_status,
)
from schemas import REMINDER_SCHEMA

logger = logging.getLogger(__name__)


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

    # Smart NLP parsing with AI (structured output)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        api_key = await get_api_key(context, user_id)
        provider_name = context.user_data.get("active_provider", "gemini")
        chat = ChatSession(provider_name=provider_name, api_key=api_key, model_name=None)
        await chat.start_chat()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        parse_prompt = (
            f"Parse this reminder request into a structured format. Current time is {now}.\n"
            f"Input: \"{text}\"\n"
            "Extract the reminder text, datetime, and whether it's recurring."
        )
        parsed, _usage = await chat.one_shot_structured(parse_prompt, REMINDER_SCHEMA)

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
    raw = _safe_callback_data(query.data)
    if raw is None:
        return REMINDERS_MENU
    reminder_id = int(raw)

    pool = _get_pool(context)
    await delete_reminder(pool, update.effective_user.id, reminder_id)
    await open_reminders_menu(update, context)
    return REMINDERS_MENU


async def check_reminders_task():
    """Background task to check for due reminders."""
    from handlers.tasks import _application

    if not _application:
        return

    pool = _application.bot_data.get("db_pool")
    if not pool:
        return

    try:
        reminders = await get_pending_reminders(pool)
    except Exception as exc:
        logger.error(f"Failed to fetch pending reminders: {exc}")
        return

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
                except Exception as recur_err:
                    logger.error(f"Failed to schedule recurring reminder: {recur_err}")

            await update_reminder_status(pool, r['id'], 'completed')
        except Exception as e:
            logger.error(f"Failed to send reminder: {e}")
