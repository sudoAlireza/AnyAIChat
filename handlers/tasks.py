"""Task management handlers — extracted from bot/conversation_handlers.py."""

import asyncio
import re
import json
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest

from handlers.common import restricted, _, _get_pool, get_api_key, _safe_callback_data
from handlers.states import (
    TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_DAYS,
    TASKS_ADD_TIME, TASKS_ADD_INTERVAL, TASKS_CONFIRM_PLAN,
)
from chat.session import ChatSession
from database.database import (
    create_task, get_user_tasks, get_user, get_user_task_hashtags,
    delete_task_by_hashtag, mark_task_completed, _generate_hashtag, record_token_usage,
    get_task_by_id, update_task_last_delivered_day,
)
from helpers.helpers import strip_markdown, split_message

logger = logging.getLogger(__name__)

# Global reference to scheduler and application for task scheduling
_scheduler = None
_application = None


def set_scheduler(scheduler, application):
    global _scheduler, _application
    _scheduler = scheduler
    _application = application


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
    await query.edit_message_text(_("Generating plan..."))
    api_key = await get_api_key(context, user_id)
    provider_name = context.user_data.get("active_provider", "gemini")
    model_name = context.user_data.get("model_name")
    chat = ChatSession(provider_name=provider_name, api_key=api_key, model_name=model_name)
    await chat.start_chat()
    parsed = await chat.generate_plan(prompt, num_days=num_days)

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
        provider_name = context.user_data.get("active_provider", "gemini")
        model_name = context.user_data.get("model_name", "unknown")
        error_text = _(
            f"Failed to generate a valid plan.\n\n"
            f"The current model ({model_name}) may not support structured output well. "
            f"Try switching to a more capable model (e.g. Gemini Flash, GPT-4o, or Claude Sonnet)."
        )
        keyboard = [
            [InlineKeyboardButton(_("🔄 Try Again"), callback_data="Tasks_Add")],
            [InlineKeyboardButton(_("🔙 Back to Tasks"), callback_data="Tasks_Menu")],
        ]
        await query.edit_message_text(error_text, reply_markup=InlineKeyboardMarkup(keyboard))
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
    hashtag = _safe_callback_data(query.data)
    if hashtag is None:
        return TASKS_MENU

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
    hashtag = _safe_callback_data(query.data)
    if hashtag is None:
        return TASKS_MENU

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


def _build_target_prompt(prompt: str, plan_json: str | None, last_delivered_day: int) -> tuple[str, int | None, int | None]:
    """Build the AI prompt for a task, returning (target_prompt, current_day, plan_total).

    Uses last_delivered_day to determine the next day to deliver (last_delivered_day + 1).
    Returns current_day=None if there is no plan.
    Returns (None, current_day, plan_total) if the plan is complete (caller should handle).
    """
    if not plan_json:
        return prompt, None, None

    try:
        plan = json.loads(plan_json)
        plan_total = len(plan)
        current_day = last_delivered_day + 1

        day_item = next((item for item in plan if item['day'] == current_day), None)
        if day_item is None and current_day > plan_total:
            # Signal: plan is complete
            return None, current_day, plan_total
        elif day_item:
            phase = day_item.get('phase', '')
            phase_info = f" Phase: {phase}." if phase else ""
            target_prompt = (
                f"You are delivering Day {current_day}/{plan_total} of a structured learning plan.{phase_info}\n"
                f"Today's title: {day_item['title']}\n"
                f"Today's goal: {day_item['subject']}\n"
                f"Overall topic: {prompt}\n\n"
                f"Provide today's content in a clear, engaging format. "
                f"Start with a brief recap connection to yesterday, then deliver today's material. "
                f"End with a quick action item or reflection question."
            )
            return target_prompt, current_day, plan_total
        else:
            return f"Plan finished or day {current_day} not found. Original prompt: {prompt}", current_day, plan_total
    except Exception as e:
        logger.error(f"Error in plan processing: {e}")
        return prompt, None, None


async def _generate_task_content(task_id: int, user_id: int, target_prompt: str, pool, user) -> tuple:
    """Generate AI content for a task. Returns (response, chat) or (None, None) on failure."""
    from database.database import get_user_api_key, get_user_provider_settings

    provider_name = user.get('active_provider', 'gemini') if user else 'gemini'
    api_key = None
    model_name = None
    if pool:
        key_row = await get_user_api_key(pool, user_id, provider_name)
        if key_row and key_row.get("api_key"):
            api_key = key_row["api_key"]
        prov_settings = await get_user_provider_settings(pool, user_id, provider_name)
        if prov_settings and prov_settings.get("model_name"):
            model_name = prov_settings["model_name"]
    if not api_key and user:
        api_key = user.get('api_key')
    if not model_name and user:
        model_name = user.get('model_name')

    # Try generating AI response with retries
    response = None
    chat = None
    max_retries = 3
    retry_delay = 5

    for attempt in range(1, max_retries + 1):
        try:
            chat = ChatSession(
                provider_name=provider_name, api_key=api_key, model_name=model_name,
                web_search=bool(user.get('grounding')) if user else False,
            )
            await chat.start_chat()
            response = await chat.one_shot(target_prompt)
            break
        except Exception as e:
            logger.warning(f"Task {task_id} attempt {attempt}/{max_retries} failed ({provider_name}/{model_name}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)

    # Fallback to Gemini if primary provider failed
    if not response or not response.text:
        if provider_name != "gemini" and pool:
            from database.database import get_user_api_key as _get_api_key
            gemini_key_row = await _get_api_key(pool, user_id, "gemini")
            gemini_key = gemini_key_row["api_key"] if gemini_key_row and gemini_key_row.get("api_key") else None
            if not gemini_key and user:
                gemini_key = user.get("api_key")
            if gemini_key:
                logger.info(f"Task {task_id}: falling back to Gemini")
                try:
                    chat = ChatSession(
                        provider_name="gemini", api_key=gemini_key, model_name="gemini-2.0-flash",
                        web_search=bool(user.get('grounding')) if user else False,
                    )
                    await chat.start_chat()
                    response = await chat.one_shot(target_prompt)
                except Exception as fb_err:
                    logger.error(f"Task {task_id}: Gemini fallback also failed: {fb_err}")

    return response, chat


async def _send_task_result(task_id, user_id, prompt, response, chat, days_passed, plan_total, task_hashtag, pool):
    """Send the generated task content to the user."""
    if response.usage and pool:
        await record_token_usage(
            pool, user_id, response.usage.get("prompt_tokens", 0),
            response.usage.get("completion_tokens", 0),
            response.usage.get("total_tokens", 0),
            model_name=chat.model_name,
            cached_tokens=response.usage.get("cached_tokens", 0),
            thinking_tokens=response.usage.get("thinking_tokens", 0),
        )
    if days_passed and plan_total:
        header = f"📬 *Day {days_passed}/{plan_total}* {task_hashtag}\n_{prompt[:50]}_\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    else:
        header = f"📬 {task_hashtag}\n_{prompt[:50]}_\n━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    parts = split_message(header + response.text)

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


async def _run_task_delivery(task_id, user_id, prompt, plan_json, task_hashtag, pool):
    """Core task delivery: build prompt, generate content, send result, track progress.

    Returns True on success, False on failure.
    On failure, does NOT increment last_delivered_day so the same day is retried.
    """
    # Load current last_delivered_day from DB (always fresh)
    task = await get_task_by_id(pool, task_id)
    if not task:
        logger.error(f"Task {task_id}: not found in DB")
        return False

    last_delivered_day = task.get("last_delivered_day", 0)
    target_prompt, current_day, plan_total = _build_target_prompt(prompt, plan_json, last_delivered_day)

    # Plan complete
    if target_prompt is None and current_day and plan_total:
        await mark_task_completed(pool, task_id)
        if _scheduler:
            try:
                _scheduler.remove_job(str(task_id))
            except Exception:
                pass
        plan = json.loads(plan_json) if plan_json else []
        await _application.bot.send_message(
            chat_id=user_id,
            text=f"🎉 *Plan Complete!* {task_hashtag}\nYour {len(plan)}-day plan on _{prompt[:40]}_ has finished.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    user = await get_user(pool, user_id) if pool else None
    response, chat = await _generate_task_content(task_id, user_id, target_prompt, pool, user)

    if not response or not response.text:
        return False

    # Success — increment last_delivered_day so next run delivers the next day
    if current_day is not None:
        await update_task_last_delivered_day(pool, task_id, current_day)

    await _send_task_result(task_id, user_id, prompt, response, chat, current_day, plan_total, task_hashtag, pool)
    return True


def schedule_task_job(task_id, user_id, prompt, run_time, interval, plan_json=None, start_date=None, hashtag=None):
    if not _scheduler:
        return

    interval = _normalize_interval(interval)
    task_hashtag = hashtag or f"#Task{task_id}"
    hour, minute = map(int, run_time.split(":"))

    async def task_wrapper():
        if not _application:
            logger.error(f"Task {task_id}: _application is None, cannot execute")
            return
        pool = _application.bot_data.get("db_pool")
        if not pool:
            return

        success = await _run_task_delivery(task_id, user_id, prompt, plan_json, task_hashtag, pool)

        if not success:
            # Load fresh state to show accurate day info
            task = await get_task_by_id(pool, task_id)
            last_delivered = task.get("last_delivered_day", 0) if task else 0
            plan_total = len(json.loads(plan_json)) if plan_json else None
            current_day = last_delivered + 1
            day_info = f" (Day {current_day}/{plan_total})" if plan_total else ""

            logger.error(f"Task {task_id}: all generation attempts failed, notifying user")
            error_text = (
                f"⚠️ {task_hashtag}{day_info}\n"
                f"Failed to generate today's content for _{prompt[:50]}_.\n\n"
                f"This can happen due to temporary API issues or model limitations. "
                f"The task will try again at the next scheduled time."
            )
            retry_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(_("🔄 Try Again"), callback_data=f"TASK_RETRY#{task_id}")]
            ])
            try:
                await _application.bot.send_message(
                    chat_id=user_id, text=error_text,
                    parse_mode=ParseMode.MARKDOWN, reply_markup=retry_keyboard,
                )
            except BadRequest:
                await _application.bot.send_message(
                    chat_id=user_id, text=strip_markdown(error_text),
                    reply_markup=retry_keyboard,
                )

    job_id = str(task_id)
    _scheduler.add_job(task_wrapper, 'cron', day_of_week=interval, hour=hour, minute=minute, id=job_id, replace_existing=True)


async def retry_task_handler(update, context):
    """Handle the 'Try Again' button on a failed task delivery."""
    query = update.callback_query
    await query.answer()

    raw = _safe_callback_data(query.data)
    if raw is None:
        return
    try:
        task_id = int(raw)
    except (ValueError, TypeError):
        return

    if not _application:
        return

    pool = _application.bot_data.get("db_pool")
    if not pool:
        return

    task = await get_task_by_id(pool, task_id)
    if not task:
        await query.edit_message_text(_("⚠️ Task not found."))
        return

    user_id = task["user_id"]
    if update.effective_user.id != user_id:
        await query.answer(_("This is not your task."), show_alert=True)
        return

    prompt = task["prompt"]
    plan_json = task.get("plan_json")
    task_hashtag = task.get("hashtag") or f"#Task{task_id}"
    last_delivered = task.get("last_delivered_day", 0)
    plan_total = len(json.loads(plan_json)) if plan_json else None
    current_day = last_delivered + 1

    # Show loading state
    await query.edit_message_text(_("🔄 Retrying..."))

    success = await _run_task_delivery(task_id, user_id, prompt, plan_json, task_hashtag, pool)

    if not success:
        day_info = f" (Day {current_day}/{plan_total})" if plan_total else ""
        error_text = (
            f"⚠️ {task_hashtag}{day_info}\n"
            f"Retry failed for _{prompt[:50]}_.\n\n"
            f"The task will try again at the next scheduled time."
        )
        retry_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(_("🔄 Try Again"), callback_data=f"TASK_RETRY#{task_id}")]
        ])
        try:
            await query.edit_message_text(error_text, parse_mode=ParseMode.MARKDOWN, reply_markup=retry_keyboard)
        except BadRequest:
            await query.edit_message_text(strip_markdown(error_text), reply_markup=retry_keyboard)
        return

    # Success — update the error message
    day_info = f" (Day {current_day}/{plan_total})" if plan_total else ""
    try:
        await query.edit_message_text(f"✅ {task_hashtag}{day_info} — content delivered.", parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        pass
