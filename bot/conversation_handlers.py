import io
import os
import logging
import uuid
import math
import json
import asyncio
from functools import wraps
from datetime import datetime


from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest
import PIL.Image

from core import GeminiChat
from database.database import (
    create_conversation,
    get_user_conversation_count,
    select_conversations_by_user,
    select_conversation_by_id,
    delete_conversation_by_id,
    create_task,
    get_user_tasks,
    delete_task_by_id,
)
from helpers.inline_paginator import InlineKeyboardPaginator
from helpers.helpers import conversations_page_content, strip_markdown, split_message, escape_markdown_v2


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

CHOOSING, IMAGE_CHOICE, CONVERSATION, CONVERSATION_HISTORY, IMAGE_CONVERSATION, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_TIME, TASKS_ADD_INTERVAL, SETTINGS_MENU, MODELS_MENU, STORAGE_MENU = range(12)

# Global reference to scheduler and application for task scheduling
_scheduler = None
_application = None

def set_scheduler(scheduler, application):
    global _scheduler, _application
    _scheduler = scheduler
    _application = application

def restricted(func):
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        auth_env = os.getenv("AUTHORIZED_USER", "")
        if not auth_env:
            return await func(update, context, *args, **kwargs)
            
        authorized_users = [int(u.strip()) for u in auth_env.split(',') if u.strip()]
        if authorized_users and user_id not in authorized_users:
            logger.info(f"Unauthorized access denied for {user_id}.")
            if update.message:
                await update.message.reply_text("This is a personal GeminiBot. You are not authorized.")
            elif update.callback_query:
                await update.callback_query.answer("Unauthorized.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)

    return wrapped


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation with /start command and ask the user for input."""
    logger.info("Received command: /start")

    keyboard = [
        [
            InlineKeyboardButton(
                _("Start New Conversation"), callback_data="New_Conversation"
            ),
        ],
        [
            InlineKeyboardButton(_("Chat History"), callback_data="PAGE#1"),
            InlineKeyboardButton(_("Tasks"), callback_data="Tasks_Menu"),
        ],
        [
            InlineKeyboardButton(_("⚙️ Settings"), callback_data="Settings_Menu"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = _("Hi. It's Gemini Chat Bot. You can ask me anything and talk to me about what you want")
    
    if update.message:
        await update.message.reply_text(text=welcome_text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text=welcome_text, reply_markup=reply_markup)

    return CHOOSING


@restricted
async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """Close current chat and return to main menu."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    
    # Save conversation if requested
    gemini_chat = context.user_data.get("gemini_chat")
    if gemini_chat and query and "_SAVE" in query.data:
        history = gemini_chat.get_chat_history()
        title = gemini_chat.get_chat_title()
        conv_id = context.user_data.get("conversation_id") or f"conv{uuid.uuid4().hex[:6]}"
        
        create_conversation(conn, (conv_id, user_id, title, json.dumps(history)))
        logger.info(f"Conversation {conv_id} saved for user {user_id}")

    # Clean up context
    context.user_data["gemini_chat"] = None
    context.user_data["gemini_image_chat"] = None
    context.user_data["conversation_id"] = None
    
    return await start(update, context)


@restricted
async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user to start conversation."""
    query = update.callback_query
    await query.answer()

    logger.info("Received callback: New_Conversation")
    
    conv_id = context.user_data.get("conversation_id")
    message_content = _("You asked for a continue conversation. OK, Let's go!") if conv_id else _("You asked for a conversation. OK, Let's start conversation!")

    keyboard = [[InlineKeyboardButton(_("Return to menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=message_content, reply_markup=reply_markup)

    return CONVERSATION


@restricted
async def reply_and_new_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send user message to Gemini and respond."""
    message = update.message
    if not message:
        return CONVERSATION

    text = message.text or message.caption
    if text and len(text) > 4000:
        await message.reply_text(_("Message is too long."))
        return CONVERSATION

    msg = await message.reply_text(_("Wait for response processing..."))

    try:
        gemini_chat = context.user_data.get("gemini_chat")
        if not gemini_chat:
            conv_id = context.user_data.get("conversation_id")
            history = []
            if conv_id:
                from main import conn
                conv_data = select_conversation_by_id(conn, (update.effective_user.id, conv_id))
                if conv_data and conv_data.get('history'):
                    history = json.loads(conv_data['history'])
            
            model_name = context.user_data.get("model_name")
            tools = []
            if context.user_data.get("web_search"):
                tools.append("google_search")
            
            gemini_chat = GeminiChat(os.getenv("GEMINI_API_TOKEN"), chat_history=history, model_name=model_name, tools=tools)
            gemini_chat.start_chat()
            context.user_data["gemini_chat"] = gemini_chat

        # Handle Multimodal Inputs
        image = None
        file_path = None
        file_mime_type = None
        prompt = text

        if message.photo:
            photo = message.photo[-1]
            photo_file = await photo.get_file()
            buf = io.BytesIO()
            await photo_file.download_to_memory(buf)
            buf.seek(0)
            image = PIL.Image.open(buf)
            if not prompt:
                prompt = "Describe this image"

        elif message.voice:
            voice = message.voice
            file = await context.bot.get_file(voice.file_id)
            voice_path = f"data/voice_{voice.file_id}.ogg"
            await file.download_to_drive(voice_path)
            file_path = voice_path
            file_mime_type = "audio/ogg"
            if not prompt:
                prompt = "Please transcribe and answer this voice message."
            else:
                prompt = f"Please transcribe and answer this voice message. Additional text: {text}"

        elif message.document:
            doc = message.document
            file = await context.bot.get_file(doc.file_id)
            file_ext = os.path.splitext(doc.file_name)[1] if doc.file_name else ""
            file_path = f"data/doc_{doc.file_id}{file_ext}"
            await file.download_to_drive(file_path)
            file_mime_type = doc.mime_type
            if not prompt:
                prompt = "Summarize this document"

        if not prompt and not image and not file_path:
             await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)
             return CONVERSATION

        response_text = gemini_chat.send_message(prompt, image=image, file_path=file_path, file_mime_type=file_mime_type)
        
        # Cleanup temp files
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        
        # Save auto-incrementally or on exit? Let's update history in context.
        # Actually, let's keep it in the GeminiChat instance.

        keyboard = [
            [InlineKeyboardButton(_("Save and Back to menu"), callback_data="Start_Again_SAVE")],
            [InlineKeyboardButton(_("Back to menu without saving"), callback_data="Start_Again")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Split long responses
        parts = split_message(response_text)
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)
        
        for i, part in enumerate(parts):
            is_last = (i == len(parts) - 1)
            markup = reply_markup if is_last else None
            try:
                await update.message.reply_text(text=part, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
            except BadRequest:
                await update.message.reply_text(text=strip_markdown(part), reply_markup=markup)

    except Exception as e:
        logger.error(f"Error in reply_and_new_message: {e}")
        await update.message.reply_text(_("An error occurred."))

    return CONVERSATION


@restricted
async def get_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """Retrieve a specific conversation."""
    try:
        conv_id = update.message.text.strip().replace("/", "")
        user_id = update.effective_user.id
        
        conversation = select_conversation_by_id(conn, (user_id, conv_id))
        if not conversation:
            await update.message.reply_text(_("Conversation not found."))
            return CONVERSATION_HISTORY

        context.user_data["conversation_id"] = conv_id
        
        keyboard = [
            [InlineKeyboardButton(_("Continue Conversations"), callback_data="New_Conversation")],
            [InlineKeyboardButton(_("Delete Conversation"), callback_data="Delete_Conversation")],
            [InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            text=_(f"Conversation {conv_id} retrieved. Title: {conversation.get('title')}"),
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in get_conversation_handler: {e}")
    return CONVERSATION_HISTORY


@restricted
async def delete_conversation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """Delete current conversation."""
    query = update.callback_query
    await query.answer()
    
    conv_id = context.user_data.get("conversation_id")
    if conv_id:
        delete_conversation_by_id(conn, (update.effective_user.id, conv_id))
        await query.edit_message_text(_("Deleted. Back to menu."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Menu"), callback_data="Start_Again")]]))
    return CHOOSING


@restricted
async def get_conversation_history(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """List conversations."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    page_number = int(query.data.split("#")[1])
    
    count = get_user_conversation_count(conn, user_id)
    total_pages = math.ceil(count / 10) if count > 0 else 1
    
    conversations = select_conversations_by_user(conn, (user_id, (page_number - 1) * 10))
    content = conversations_page_content(conversations) if conversations else _("No history.")
    
    paginator = InlineKeyboardPaginator(total_pages, current_page=page_number, data_pattern="PAGE#{page}")
    paginator.add_after(InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again"))
    
    await query.edit_message_text(text=content, reply_markup=paginator.markup, parse_mode=ParseMode.MARKDOWN)
    return CONVERSATION_HISTORY


# --- End of Handlers ---


# --- Tasks Handlers ---

@restricted
async def open_tasks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton(_("➕ Add New Task"), callback_data="Tasks_Add")],
        [InlineKeyboardButton(_("📋 List Tasks"), callback_data="Tasks_List")],
        [InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(_("Tasks Menu"), reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_MENU

@restricted
async def start_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(_("Enter task prompt:"))
    return TASKS_ADD_PROMPT

@restricted
async def handle_task_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["task_prompt"] = update.message.text
    await update.message.reply_text(_("Enter time (HH:MM):"))
    return TASKS_ADD_TIME

@restricted
async def handle_task_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text
    try:
        datetime.strptime(time_str, "%H:%M")
        context.user_data["task_time"] = time_str
        
        keyboard = [
            [InlineKeyboardButton(_("Once"), callback_data="Tasks_Interval_once")],
            [InlineKeyboardButton(_("Daily"), callback_data="Tasks_Interval_daily")],
            [InlineKeyboardButton(_("Weekly"), callback_data="Tasks_Interval_weekly")],
        ]
        await update.message.reply_text(_("Choose interval:"), reply_markup=InlineKeyboardMarkup(keyboard))
        return TASKS_ADD_INTERVAL
    except ValueError:
        await update.message.reply_text(_("Invalid format. Use HH:MM:"))
        return TASKS_ADD_TIME

@restricted
async def handle_task_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    query = update.callback_query
    await query.answer()
    interval = query.data.split("_")[-1]
    
    user_id = update.effective_user.id
    prompt = context.user_data.get("task_prompt")
    run_time = context.user_data.get("task_time")
    
    task_id = create_task(conn, (user_id, prompt, run_time, interval))
    
    schedule_task_job(task_id, user_id, prompt, run_time, interval)
    
    await query.edit_message_text(_("Task scheduled!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]))
    return TASKS_MENU

@restricted
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    query = update.callback_query
    await query.answer()
    
    tasks = get_user_tasks(conn, update.effective_user.id)
    if not tasks:
        await query.edit_message_text(_("No tasks."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="Tasks_Menu")]]))
        return TASKS_MENU
        
    text = _("Your tasks:\n")
    keyboard = []
    for t in tasks:
        text += f"ID: {t['id']} | {t['run_time']} | {t['interval']} | {t['prompt'][:20]}...\n"
        keyboard.append([InlineKeyboardButton(_(f"Delete Task #{t['id']}"), callback_data=f"TASK_DELETE#{t['id']}")])
    
    keyboard.append([InlineKeyboardButton(_("Back"), callback_data="Tasks_Menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return TASKS_MENU

@restricted
async def delete_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    query = update.callback_query
    await query.answer()
    task_id = int(query.data.split("#")[1])
    
    if delete_task_by_id(conn, (update.effective_user.id, task_id)):
        if _scheduler:
            try:
                _scheduler.remove_job(str(task_id))
            except:
                pass
        await query.edit_message_text(_("Task deleted."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="Tasks_Menu")]]))
    return TASKS_MENU

def schedule_task_job(task_id, user_id, prompt, run_time, interval):
    if not _scheduler:
        return
        
    hour, minute = map(int, run_time.split(":"))
    
    async def task_wrapper():
        gemini = GeminiChat(os.getenv("GEMINI_API_TOKEN"))
        gemini.start_chat()
        response = gemini.send_message(prompt)
        parts = split_message(f"Scheduled Task Result:\nPrompt: {prompt}\n\n{response}")
        for part in parts:
            await _application.bot.send_message(chat_id=user_id, text=part)

    job_id = str(task_id)
    if interval == "once":
        _scheduler.add_job(task_wrapper, 'cron', hour=hour, minute=minute, id=job_id, replace_existing=True)
    elif interval == "daily":
        _scheduler.add_job(task_wrapper, 'cron', hour=hour, minute=minute, id=job_id, replace_existing=True)
    elif interval == "weekly":
        _scheduler.add_job(task_wrapper, 'cron', day_of_week='mon', hour=hour, minute=minute, id=job_id, replace_existing=True)

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start_over(update, context, None)


# --- Settings Handlers ---

@restricted
async def open_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    current_model = context.user_data.get("model_name") or os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    web_search = context.user_data.get("web_search", False)
    
    ws_status = "✅ Enabled" if web_search else "❌ Disabled"
    
    keyboard = [
        [InlineKeyboardButton(f"🤖 Model: {current_model}", callback_data="open_models_menu")],
        [InlineKeyboardButton(f"🌐 Web Search: {ws_status}", callback_data="TOGGLE_WEB_SEARCH")],
        [InlineKeyboardButton(_("📁 Storage Management"), callback_data="Storage_Menu")],
        [InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")],
    ]
    await query.edit_message_text(_("Settings Menu"), reply_markup=InlineKeyboardMarkup(keyboard))
    return SETTINGS_MENU

@restricted
async def open_models_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show a menu of all available models from Google AI."""
    query = update.callback_query
    await query.answer()
    
    models = GeminiChat.list_models()
    if not models:
        await query.edit_message_text(_("Failed to fetch models or no models available."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]))
        return SETTINGS_MENU
        
    current_model = context.user_data.get("model_name") or os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    
    keyboard = []
    for m in models:
        prefix = "✅ " if m['name'].endswith(current_model) or m['name'] == current_model else ""
        keyboard.append([InlineKeyboardButton(f"{prefix}{m['display_name']}", callback_data=f"SET_MODEL_{m['name']}")])
    
    keyboard.append([InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")])
    await query.edit_message_text(_("Choose a Gemini Model:"), reply_markup=InlineKeyboardMarkup(keyboard))
    return MODELS_MENU

@restricted
async def set_model_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    model_name = query.data.replace("SET_MODEL_", "")
    context.user_data["model_name"] = model_name
    await open_models_menu(update, context)
    return MODELS_MENU

@restricted
async def open_storage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show files currently stored in Gemini's temporary storage."""
    query = update.callback_query
    await query.answer()
    
    files = GeminiChat.list_uploaded_files()
    if not files:
        content = _("No files currently stored in Gemini's temporary storage.")
    else:
        content = _("Active files in Google's temporary storage (expire after 48h):\n\n")
        for f in files:
            size_mb = f['size_bytes'] / (1024 * 1024)
            content += f"• `{f['display_name']}` ({f['mime_type']}, {size_mb:.2f} MB)\n"
    
    keyboard = [[InlineKeyboardButton(_("Back"), callback_data="Settings_Menu")]]
    await query.edit_message_text(content, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return STORAGE_MENU

@restricted
async def toggle_web_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    current = context.user_data.get("web_search", False)
    context.user_data["web_search"] = not current
    await open_settings_menu(update, context)
    return SETTINGS_MENU