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
        [InlineKeyboardButton(_("Manage Tasks"), callback_data="Manage_Tasks")],
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
        [InlineKeyboardButton(_("Manage Tasks"), callback_data="Manage_Tasks")],
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