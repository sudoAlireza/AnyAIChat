import io
import os
import logging
import uuid
import math
import pickle
from functools import wraps


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
)
from helpers.inline_paginator import InlineKeyboardPaginator
from helpers.helpers import conversations_page_content, strip_markdown


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, IMAGE_CHOICE, CONVERSATION, CONVERSATION_HISTORY, IMAGE_CONVERSATION = range(5)


def restricted(func):
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        authorized_users = [int(user_id.strip()) for user_id in os.getenv("AUTHORIZED_USER", "").split(',')]
        if user_id not in authorized_users:
            logger.info(f"Unauthorized access denied for {user_id}.")
            await update.message.reply_animation(
                "https://github.com/sudoAlireza/GeminiBot/assets/87416117/beeb0fd2-73c6-4631-baea-2e3e3eeb9319",
                caption="This is my persoanl GeminiBot, to run your own Bot look at:\nhttps://github.com/sudoAlireza/GeminiBot",
            )
            return
        return await func(update, context, *args, **kwargs)

    return wrapped


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation with /start command and ask the user for input."""
    query = update.callback_query
    logger.info("Received command: /start")

    keyboard = [
        [
            InlineKeyboardButton(
                _("Start New Conversation"), callback_data="New_Conversation"
            ),
            InlineKeyboardButton(
                _("Image Description"), callback_data="Image_Description"
            ),
        ],
        [InlineKeyboardButton(_("Chat History"), callback_data="PAGE#1")],
        [InlineKeyboardButton(_("📅 Tasks"), callback_data="Tasks_Menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        text=_("Hi. It's Gemini Chat Bot. You can ask me anything and talk to me about what you want"),
        reply_markup=reply_markup,
    )

    return CHOOSING


@restricted
async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """Start the conversation with button and ask the user for input."""
    query = update.callback_query
    if query:
        await query.answer()

    prev_message = context.user_data.get("to_delete_message")
    if prev_message:
        try:
            await context.bot.delete_message(
                chat_id=prev_message.chat_id, message_id=prev_message.id
            )
        except BadRequest:
            pass  # Ignore if the message is already deleted
        context.user_data["to_delete_message"] = None

    try:
        user_details = update.effective_user
        user_id = user_details.id
        conversation_id = context.user_data.get("conversation_id")
        gemini_chat: GeminiChat = context.user_data.get("gemini_chat")
        gemini_image_chat: GeminiChat = context.user_data.get("gemini_image_chat")

        if gemini_chat or conversation_id:
            if query and "_SAVE" in query.data:
                conversation_history = gemini_chat.get_chat_history()
                conversation_title = gemini_chat.get_chat_title()

                conversation_id = conversation_id or f"conv{uuid.uuid4().hex[:6]}"
                with open(f"./pickles/{conversation_id}.pickle", "wb") as fp:
                    pickle.dump(conversation_history, fp)

                conv = (
                    conversation_id,
                    user_id,
                    conversation_title,
                )
                create_conversation(conn, conv)
                logger.info(f"conversation {conversation_id} saved in db and closed")

            else:
                logger.info(f"conversation {conversation_id} closed without saving")

            gemini_chat.close()

        if gemini_image_chat:
            gemini_image_chat.close()

        else:
            logger.info("No active chat to close")

        gemini_chat = None
        context.user_data["gemini_chat"] = None
        context.user_data["gemini_image_chat"] = None
        context.user_data["conversation_id"] = None

    except Exception as e:
        logger.error("Error during conversation handling: %s", e)

    keyboard = [
        [
            InlineKeyboardButton(
                _("Start New Conversation"), callback_data="New_Conversation"
            )
        ],
        [InlineKeyboardButton(_("Image Description"), callback_data="Image_Description")],
        [InlineKeyboardButton(_("Chat History"), callback_data="PAGE#1")],
        [InlineKeyboardButton(_("📅 Tasks"), callback_data="Tasks_Menu")],
        [InlineKeyboardButton(_("Start Again"), callback_data="Start_Again")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await context.bot.send_message(
        update.effective_chat.id,
        text=_("Hi. It's Gemini Chat Bot. You can ask me anything and talk to me about what you want"),
        reply_markup=reply_markup,
    )
    context.user_data["to_delete_message"] = msg

    return CHOOSING


@restricted
async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user to start conversation by writing any message."""
    query = update.callback_query
    await query.answer()

    logger.info("Received callback: New_Conversation")
    message_content = _("You asked for a conversation. OK, Let's start conversation!")

    conv_id = context.user_data.get("conversation_id")
    if conv_id:
        message_content = _("You asked for a continue conversation. OK, Let's go!")

    keyboard = [[InlineKeyboardButton(_("Return to menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = await query.edit_message_text(
        text=message_content,
        reply_markup=reply_markup,
    )
    context.user_data["to_delete_message"] = msg

    return CONVERSATION


@restricted
async def reply_and_new_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Send user message to Gemini core and respond and wait for new message or exit command"""
    keyboard = [[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if len(update.message.text) > 2048:
        await update.message.reply_text(_("Message is too long. Please shorten your message and try again."))
        return CONVERSATION

    msg = await update.message.reply_text(
        text=_("Wait for response processing..."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )

    try:
        text = update.message.text
        conv_id = context.user_data.get("conversation_id")
        conversation_history = []
        if conv_id:
            try:
                with open(f"./pickles/{conv_id}.pickle", "rb") as fp:
                    conversation_history = pickle.load(fp)
            except FileNotFoundError:
                logger.warning(f"Pickle file for conversation {conv_id} not found.")
                # Handle the case where the pickle file is missing
                # For example, send a message to the user and offer to start a new conversation
                await update.message.reply_text(_("Sorry, I couldn't find the history for this conversation. Let's start a new one."))
                return await start_over(update, context, None)


        gemini_chat = context.user_data.get("gemini_chat")
        if not gemini_chat:
            logger.info("Creating new conversation instance")
            gemini_chat = GeminiChat(
                gemini_token=os.getenv("GEMINI_API_TOKEN"),
                chat_history=conversation_history,
            )
            gemini_chat.start_chat()

        response = gemini_chat.send_message(text).encode("utf-8").decode("utf-8", "ignore")
        context.user_data["gemini_chat"] = gemini_chat

        keyboard = [
            [
                InlineKeyboardButton(
                    _("Save and Back to menu"), callback_data="Start_Again_SAVE"
                )
            ],
            [
                InlineKeyboardButton(
                    _("Back to menu without saving"), callback_data="Start_Again"
                )
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await context.bot.send_message(
                text=response,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                chat_id=update.message.chat_id,
            )
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)

        except Exception as e:
            await context.bot.send_message(
                text=strip_markdown(response),
                reply_markup=reply_markup,
                chat_id=update.message.chat_id,
            )
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)
            logging.warning(f"Error sending message: {e}")

    except Exception as e:
        logger.error(f"Error in reply_and_new_message: {e}")
        if "429" in str(e):
            await update.message.reply_text(_("You have exceeded your daily quota for requests. Please try again later."))
        else:
            await update.message.reply_text(_("An error occurred while processing your message."))

    return CONVERSATION


@restricted
async def get_conversation_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, conn
) -> int:
    """Get conversation from database and ask user if wants new conversation or not"""
    try:
        query_messsage = update.message.text.replace("/", "")
        context.user_data["conversation_id"] = query_messsage
        user_details = update.message.from_user.id
        conv_specs = (user_details, query_messsage)

        conversation = select_conversation_by_id(conn, conv_specs)

        if not conversation:
            await update.message.reply_text(_("Conversation not found."))
            return CONVERSATION_HISTORY

        message_content = _(f"Conversation {conversation.get('conv_id')} retrieved and title is: {conversation.get('title')}")

        keyboard = [
            [
                InlineKeyboardButton(
                    _("Continue Conversations"), callback_data="New_Conversation"
                )
            ],
            [
                InlineKeyboardButton(
                    _("Delete Conversation"), callback_data="Delete_Conversation"
                )
            ],
            [InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await update.message.reply_text(
            text=message_content, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN
        )
        context.user_data["to_delete_message"] = msg

    except Exception as e:
        logger.error(f"Error in get_conversation_handler: {e}")
        await update.message.reply_text(_("An error occurred while retrieving the conversation."))

    return CONVERSATION_HISTORY


@restricted
async def delete_conversation_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, conn
) -> int:
    """Delete conversation if user clicks on Delete button"""
    try:
        query = update.callback_query
        await query.answer()

        conversation_id = context.user_data.get("conversation_id")
        if not conversation_id:
            await query.edit_message_text(_("No conversation selected for deletion."))
            return CHOOSING

        user_details = query.from_user.id
        conv_specs = (user_details, conversation_id)

        deleted = delete_conversation_by_id(conn, conv_specs)

        if not deleted:
            await query.edit_message_text(_("Could not delete the conversation. It may have been already deleted."))
            return CHOOSING

        keyboard = [[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await query.edit_message_text(
            text=_("Conversation history deleted successfully. Back to menu Start new Conversation"),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["to_delete_message"] = msg

    except Exception as e:
        logger.error(f"Error in delete_conversation_handler: {e}")
        await query.edit_message_text(_("An error occurred while deleting the conversation."))

    return CHOOSING


@restricted
async def start_image_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Ask user to upload an image with caption"""
    query = update.callback_query
    await query.answer()
    logger.info("Received callback: Image_Description")

    keyboard = [[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = await query.edit_message_text(
        _(f"You asked for Image description. OK, Send your image with caption!"),
        reply_markup=reply_markup,
    )
    context.user_data["to_delete_message"] = msg

    logger.info("Returning IMAGE_CHOICE")
    return IMAGE_CHOICE


@restricted
async def generate_text_from_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Send image to Gemini core and send response to user"""
    logger.info("Entered generate_text_from_image")
    logger.info("Received image from user")
    buf = None # Initialize buf to None
    image = None # Initialize image to None
    try:
        photo = update.message.photo[-1]
        if photo.file_size > 4 * 1024 * 1024:
            await update.message.reply_text(_("Image is too large. Please send an image smaller than 4MB."))
            return IMAGE_CONVERSATION

        keyboard = [[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await update.message.reply_text(
            text=_("Wait for response processing..."),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
        )

        photo_file = await photo.get_file()
        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "user_image.jpg"
        buf.seek(0)

        image = PIL.Image.open(buf)

        gemini_image_chat = GeminiChat(
            gemini_token=os.getenv("GEMINI_API_TOKEN")
        )
        gemini_image_chat.start_chat(image=image)

        try:
            prompt = update.message.caption if update.message.caption else "Describe this image"
            response = gemini_image_chat.send_message(prompt)

            if not response:
                raise Exception(_("Empty response from Gemini"))
        except Exception as e:
            logger.warning("Error during image processing: %s", e)
            if "429" in str(e):
                response = _("You have exceeded your daily quota for requests. Please try again later.")
            else:
                response = _("Couldn't generate a response. Please try again.")

        context.user_data["gemini_image_chat"] = gemini_image_chat

        try:
            await context.bot.send_message(
                text=response,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                chat_id=update.message.chat_id,
            )
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)

        except Exception as e:
            logging.warning(f"Error sending message with markdown: {e}. Original response: {response}")
            await context.bot.send_message(
                text=strip_markdown(response),
                reply_markup=reply_markup,
                chat_id=update.message.chat_id,
            )
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)
    
    except Exception as e:
        logger.error(f"Error in generate_text_from_image: {e}")
        if "429" in str(e):
            await update.message.reply_text(_("You have exceeded your daily quota for requests. Please try again later."))
        else:
            await update.message.reply_text(_("An error occurred while processing the image."))
    finally:
        if buf:
            buf.close()
        if image:
            del image

    return IMAGE_CONVERSATION

@restricted
async def reply_to_image_conversation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Send user message to Gemini core and respond and wait for new message or exit command"""
    if len(update.message.text) > 2048:
        await update.message.reply_text(_("Message is too long. Please shorten your message and try again."))
        return IMAGE_CONVERSATION

    keyboard = [[InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = await update.message.reply_text(
        text=_("Wait for response processing..."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
    )

    try:
        text = update.message.text
        gemini_image_chat = context.user_data.get("gemini_image_chat")
        
        response = gemini_image_chat.send_message(text)
        context.user_data["gemini_image_chat"] = gemini_image_chat

        try:
            await context.bot.send_message(
                text=response,
                parse_mode="Markdown",
                reply_markup=reply_markup,
                chat_id=update.message.chat_id,
            )
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)

        except Exception as e:
            logging.warning(f"Error sending message with markdown: {e}. Original response: {response}")
            await context.bot.send_message(
                text=strip_markdown(response),
                reply_markup=reply_markup,
                chat_id=update.message.chat_id,
            )
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.id)

    except Exception as e:
        logger.error(f"Error in reply_to_image_conversation: {e}")
        if "429" in str(e):
            await update.message.reply_text(_("You have exceeded your daily quota for requests. Please try again later."))
        else:
            await update.message.reply_text(_("An error occurred while processing your message."))

    return IMAGE_CONVERSATION


@restricted
async def get_conversation_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE, conn
) -> int:
    """Read conversations history of the user"""
    try:
        query = update.callback_query
        await query.answer()
        logger.info("Received callback: PAGE#")

        user_id = query.from_user.id
        conversations_count = get_user_conversation_count(conn, user_id)
        total_pages = math.ceil(float(conversations_count / 10))

        page_number = int(query.data.split("#")[1])
        offset = (page_number - 1) * 10

        conversations = select_conversations_by_user(conn, (user_id, offset))
        if conversations:
            page_content = conversations_page_content(conversations)
        else:
            page_content = _("You have not any chat history")

        paginator = InlineKeyboardPaginator(
            total_pages, current_page=page_number, data_pattern="PAGE#{page}"
        )
        paginator.add_after(
            InlineKeyboardButton(_("Back to menu"), callback_data="Start_Again")
        )

        msg = await query.edit_message_text(
            page_content,
            reply_markup=paginator.markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["to_delete_message"] = msg

    except Exception as e:
        logger.error(f"Error in get_conversation_history: {e}")
        await query.edit_message_text(_("An error occurred while retrieving the conversation history."))

    return CONVERSATION_HISTORY


@restricted
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End the conversation."""
    # To-Do: Remove this handler and handle ending with start and start_over handlers
    query = update.callback_query
    logger.info("Received callback: Done")

    try:
        user_data = context.user_data
        if 'gemini_chat' in user_data and user_data['gemini_chat']:
            user_data['gemini_chat'].close()
    except Exception as e:
        logger.error(f"Error in done handler: {e}")

    if 'gemini_chat' in context.user_data:
        context.user_data["gemini_chat"] = None

    keyboard = [
        [
            InlineKeyboardButton(
                _("Start New Conversation"), callback_data="New_Conversation"
            )
        ],
        [InlineKeyboardButton(_("Image Description"), callback_data="Image_Description")],
        [InlineKeyboardButton(_("Refresh"), callback_data="Start_Again")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(_("Until next time!"), reply_markup=reply_markup)

    user_data.clear()
    return CHOOSING


# Task scheduling globals
_scheduler = None
_bot_application = None

TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_TIME, TASKS_ADD_INTERVAL = range(5, 9)


def set_scheduler(scheduler, application):
    """Set the global scheduler and bot application instances"""
    global _scheduler, _bot_application
    _scheduler = scheduler
    _bot_application = application


async def send_scheduled_task(user_id: int, prompt: str):
    """Execute scheduled task: send prompt to Gemini and send response to user"""
    try:
        logger.info(f"Executing scheduled task for user {user_id} with prompt: {prompt[:50]}...")
        
        gemini_chat = GeminiChat(gemini_token=os.getenv("GEMINI_API_TOKEN"))
        gemini_chat.start_chat()
        response = gemini_chat.send_message(prompt)
        gemini_chat.close()
        
        keyboard = [[InlineKeyboardButton(_("Tasks Menu"), callback_data="Tasks_Menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = f"🔔 *Scheduled Task Result*\n\n*Prompt:* {prompt}\n\n*Response:*\n{response}"
        
        try:
            await _bot_application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.warning(f"Error sending with markdown: {e}")
            await _bot_application.bot.send_message(
                chat_id=user_id,
                text=strip_markdown(message),
                reply_markup=reply_markup,
            )
            
    except Exception as e:
        logger.error(f"Error executing scheduled task: {e}")
        try:
            await _bot_application.bot.send_message(
                chat_id=user_id,
                text=_("❌ Error executing scheduled task. Please try again later."),
            )
        except Exception as send_error:
            logger.error(f"Failed to send error message: {send_error}")


def schedule_task_job(task_id: int, user_id: int, prompt: str, run_time: str, interval: str):
    """Schedule a task job with APScheduler"""
    try:
        from datetime import datetime
        
        job_id = f"task_{task_id}"
        
        # Parse run_time (format: HH:MM)
        hour, minute = map(int, run_time.split(':'))
        
        if interval == "once":
            # Schedule for next occurrence of this time
            run_date = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_date <= datetime.now():
                from datetime import timedelta
                run_date += timedelta(days=1)
            
            _scheduler.add_job(
                send_scheduled_task,
                'date',
                run_date=run_date,
                args=[user_id, prompt],
                id=job_id,
                replace_existing=True,
            )
            logger.info(f"Scheduled one-time task {job_id} for {run_date}")
            
        elif interval == "daily":
            _scheduler.add_job(
                send_scheduled_task,
                'cron',
                hour=hour,
                minute=minute,
                args=[user_id, prompt],
                id=job_id,
                replace_existing=True,
            )
            logger.info(f"Scheduled daily task {job_id} at {run_time}")
            
        elif interval == "weekly":
            _scheduler.add_job(
                send_scheduled_task,
                'cron',
                day_of_week='mon',
                hour=hour,
                minute=minute,
                args=[user_id, prompt],
                id=job_id,
                replace_existing=True,
            )
            logger.info(f"Scheduled weekly task {job_id} at {run_time}")
            
    except Exception as e:
        logger.error(f"Error scheduling task: {e}")


@restricted
async def open_tasks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Open tasks menu"""
    query = update.callback_query
    await query.answer()
    
    logger.info("Received callback: Tasks_Menu")
    
    keyboard = [
        [InlineKeyboardButton(_("➕ Add New Task"), callback_data="Tasks_Add")],
        [InlineKeyboardButton(_("📋 List Tasks"), callback_data="Tasks_List")],
        [InlineKeyboardButton(_("🔙 Back to Menu"), callback_data="Start_Again")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = await query.edit_message_text(
        text=_("📅 *Tasks Menu*\n\nManage your scheduled tasks here. You can add new tasks, view existing ones, or delete them."),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["to_delete_message"] = msg
    
    return TASKS_MENU


@restricted
async def start_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start adding a new task"""
    query = update.callback_query
    await query.answer()
    
    logger.info("Received callback: Tasks_Add")
    
    keyboard = [[InlineKeyboardButton(_("Cancel"), callback_data="Tasks_Menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = await query.edit_message_text(
        text=_("📝 *Add New Task*\n\nPlease enter the prompt you want to send to Gemini:"),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["to_delete_message"] = msg
    
    return TASKS_ADD_PROMPT


@restricted
async def handle_task_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle task prompt input"""
    prompt = update.message.text
    
    if len(prompt) > 1000:
        await update.message.reply_text(_("Prompt is too long. Please keep it under 1000 characters."))
        return TASKS_ADD_PROMPT
    
    context.user_data["task_prompt"] = prompt
    
    keyboard = [[InlineKeyboardButton(_("Cancel"), callback_data="Tasks_Menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        text=_("⏰ *Set Execution Time*\n\nPlease enter the time when you want this task to run (format: HH:MM, e.g., 09:30 or 14:00):"),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
    )
    
    return TASKS_ADD_TIME


@restricted
async def handle_task_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle task time input"""
    time_str = update.message.text.strip()
    
    # Validate time format
    import re
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        await update.message.reply_text(_("Invalid time format. Please use HH:MM format (e.g., 09:30 or 14:00):"))
        return TASKS_ADD_TIME
    
    context.user_data["task_time"] = time_str
    
    keyboard = [
        [InlineKeyboardButton(_("Once"), callback_data="Tasks_Interval_once")],
        [InlineKeyboardButton(_("Daily"), callback_data="Tasks_Interval_daily")],
        [InlineKeyboardButton(_("Weekly"), callback_data="Tasks_Interval_weekly")],
        [InlineKeyboardButton(_("Cancel"), callback_data="Tasks_Menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        text=_("🔄 *Set Interval*\n\nHow often should this task run?"),
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
    )
    
    return TASKS_ADD_INTERVAL


@restricted
async def handle_task_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """Handle task interval selection and save task"""
    query = update.callback_query
    await query.answer()
    
    interval = query.data.split("_")[-1]
    
    user_id = query.from_user.id
    prompt = context.user_data.get("task_prompt")
    run_time = context.user_data.get("task_time")
    
    try:
        from database.database import create_task
        
        task = (user_id, prompt, run_time, interval)
        task_id = create_task(conn, task)
        
        # Schedule the task
        schedule_task_job(task_id, user_id, prompt, run_time, interval)
        
        # Clear user data
        context.user_data.pop("task_prompt", None)
        context.user_data.pop("task_time", None)
        
        interval_text = {
            "once": _("once"),
            "daily": _("daily"),
            "weekly": _("weekly (every Monday)"),
        }.get(interval, interval)
        
        keyboard = [
            [InlineKeyboardButton(_("📋 View Tasks"), callback_data="Tasks_List")],
            [InlineKeyboardButton(_("🔙 Back to Menu"), callback_data="Start_Again")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = await query.edit_message_text(
            text=_(f"✅ *Task Created Successfully!*\n\n*Prompt:* {prompt}\n*Time:* {run_time}\n*Interval:* {interval_text}"),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["to_delete_message"] = msg
        
    except Exception as e:
        logger.error(f"Error creating task: {e}")
        await query.edit_message_text(_("❌ Error creating task. Please try again."))
    
    return TASKS_MENU


@restricted
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """List all tasks for the user"""
    query = update.callback_query
    await query.answer()
    
    logger.info("Received callback: Tasks_List")
    
    user_id = query.from_user.id
    
    try:
        from database.database import get_user_tasks
        
        tasks = get_user_tasks(conn, user_id)
        
        if not tasks:
            keyboard = [
                [InlineKeyboardButton(_("➕ Add Task"), callback_data="Tasks_Add")],
                [InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Menu")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            msg = await query.edit_message_text(
                text=_("📋 *Your Tasks*\n\nYou don't have any scheduled tasks yet."),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
            )
            context.user_data["to_delete_message"] = msg
            return TASKS_MENU
        
        # Build task list message
        message = _("📋 *Your Scheduled Tasks*\n\n")
        keyboard = []
        
        for task in tasks:
            interval_emoji = {"once": "🔔", "daily": "📅", "weekly": "📆"}.get(task["interval"], "⏰")
            interval_text = {
                "once": _("once"),
                "daily": _("daily"),
                "weekly": _("weekly"),
            }.get(task["interval"], task["interval"])
            
            prompt_preview = task["prompt"][:50] + "..." if len(task["prompt"]) > 50 else task["prompt"]
            message += f"{interval_emoji} *Task #{task['id']}*\n"
            message += f"   Prompt: {prompt_preview}\n"
            message += f"   Time: {task['run_time']} ({interval_text})\n\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    _(f"🗑️ Delete Task #{task['id']}"),
                    callback_data=f"TASK_DELETE#{task['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton(_("🔙 Back"), callback_data="Tasks_Menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg = await query.edit_message_text(
            text=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["to_delete_message"] = msg
        
    except Exception as e:
        logger.error(f"Error listing tasks: {e}")
        await query.edit_message_text(_("❌ Error retrieving tasks. Please try again."))
    
    return TASKS_MENU


@restricted
async def delete_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    """Delete a task"""
    query = update.callback_query
    await query.answer()
    
    task_id = int(query.data.split("#")[1])
    user_id = query.from_user.id
    
    try:
        from database.database import delete_task_by_id
        
        deleted = delete_task_by_id(conn, task_id, user_id)
        
        if deleted:
            # Remove from scheduler
            job_id = f"task_{task_id}"
            try:
                _scheduler.remove_job(job_id)
                logger.info(f"Removed scheduled job {job_id}")
            except Exception as e:
                logger.warning(f"Job {job_id} not found in scheduler: {e}")
            
            await query.answer(_("✅ Task deleted successfully!"), show_alert=True)
        else:
            await query.answer(_("❌ Task not found or already deleted."), show_alert=True)
        
        # Refresh task list
        return await list_tasks(update, context, conn)
        
    except Exception as e:
        logger.error(f"Error deleting task: {e}")
        await query.answer(_("❌ Error deleting task."), show_alert=True)
    
    return TASKS_MENU
