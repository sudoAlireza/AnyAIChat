"""Daily briefing, URL monitoring, and background task handlers."""

import hashlib
import logging
from datetime import datetime, timedelta

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool
from handlers.states import SETTINGS_MENU, BRIEFING_MENU, URL_MONITOR_MENU, URL_MONITOR_INPUT
from database.database import (
    get_user, update_user_settings,
    add_url_monitor, get_user_monitors, delete_url_monitor,
    get_active_monitors, update_monitor_hash,
    get_user_stats, get_user_tasks, get_user_reminders,
)
from helpers.helpers import strip_markdown

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependencies; set by the application at startup.
_application = None


def set_application(application):
    """Called during startup to inject the application reference."""
    global _application
    _application = application


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
