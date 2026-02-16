import os
import logging
import gettext
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    PicklePersistence,
)
from database.database import create_connection, create_table
from bot.conversation_handlers import (
    start,
    start_over,
    start_conversation,
    reply_and_new_message,
    start_image_conversation,
    generate_text_from_image,
    get_conversation_history,
    get_conversation_handler,
    delete_conversation_handler,
    done,
    reply_to_image_conversation,
)

# Setup translation
localedir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "locales")
# Determine language from environment variable, default to 'ru'
lang = os.getenv("LANGUAGE", "ru")
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
# Install _() globally
gettext.install("messages", localedir, names=("ngettext",))
gettext.translation("messages", localedir, languages=[lang], fallback=True).install()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=log_level
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

logging.info(f"Selected language: {lang}")
logging.info(f"Selected log level: {log_level}")

CHOOSING, IMAGE_CHOICE, CONVERSATION, CONVERSATION_HISTORY, IMAGE_CONVERSATION = range(5)


def entry_points():
    return [
        CommandHandler("start", lambda update, context: start(update, context)),
        CallbackQueryHandler(
            lambda update, context: start_over(update, context, conn),
            pattern="^Start_Again",
        ),
    ]


from bot.tasks import (
    show_tasks_menu, 
    add_task_start, 
    receive_prompt, 
    receive_time, 
    receive_interval, 
    list_tasks_handler, 
    delete_task_handler,
    ASK_PROMPT, 
    ASK_TIME, 
    ASK_INTERVAL
)

def states():
    return {
        CHOOSING: [
            CallbackQueryHandler(
                lambda update, context: start_conversation(update, context),
                pattern="^New_Conversation$",
            ),
            CallbackQueryHandler(
                lambda update, context: start_image_conversation(update, context),
                pattern="^Image_Description$",
            ),
            CallbackQueryHandler(
                lambda update, context: get_conversation_history(update, context, conn),
                pattern="^PAGE#",
            ),
            CallbackQueryHandler(
                lambda update, context: show_tasks_menu(update, context),
                pattern="^Manage_Tasks$",
            ),
             CallbackQueryHandler(
                lambda update, context: add_task_start(update, context),
                pattern="^Add_Task$",
            ),
            CallbackQueryHandler(
                lambda update, context: list_tasks_handler(update, context, conn),
                pattern="^List_Tasks$",
            ),
            CallbackQueryHandler(
                lambda update, context: delete_task_handler(update, context, conn),
                pattern="^Delete_Task_",
            ),
            CallbackQueryHandler(
                lambda update, context: done(update, context),
                pattern="^End_Conversation$",
            ),
        ],
        IMAGE_CHOICE: [
            MessageHandler(
                filters.PHOTO,
                generate_text_from_image,
            )
        ],
        CONVERSATION: [
            MessageHandler(
                filters.TEXT & ~filters.Regex("^/"),
                reply_and_new_message,
            )
        ],
        CONVERSATION_HISTORY: [
            CallbackQueryHandler(
                lambda update, context: get_conversation_history(update, context, conn),
                pattern="^PAGE#",
            ),
            MessageHandler(
                filters.Regex("^/conv"),
                lambda update, context: get_conversation_handler(update, context, conn),
            ),
            CallbackQueryHandler(
                lambda update, context: delete_conversation_handler(
                    update, context, conn
                ),
                pattern="^Delete_Conversation$",
            ),
        ],
        IMAGE_CONVERSATION: [
            MessageHandler(
                filters.TEXT & ~filters.Regex("^/"),
                reply_to_image_conversation,
            )
        ],
        ASK_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prompt)],
        ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time)],
        ASK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: receive_interval(u, c, conn))],
    }


def fallbacks():
    return [
        CallbackQueryHandler(
            lambda update, context: done(update, context), pattern="^Done$"
        ),
        CallbackQueryHandler(
            lambda update, context: start_over(update, context, conn),
            pattern="^Start_Again",
        ),
    ]


def create_conv_handler():
    return ConversationHandler(
        entry_points=entry_points(),
        states=states(),
        fallbacks=fallbacks(),
        persistent=True,
        name="gemini_conversation",
        per_message=False,
        allow_reentry=True,
    )


from bot.tasks import get_add_task_handler, get_task_command_handlers, load_tasks
from apscheduler.schedulers.asyncio import AsyncIOScheduler

def main() -> None:
    persistence = PicklePersistence(filepath="conversation_persistence")
    application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).persistence(persistence).build()

    conv_handler = create_conv_handler()
    application.add_handler(conv_handler)
    
    # Task Handlers
    # task_conv_handler = get_add_task_handler(conn) # Removed as integrated into main conv
    # application.add_handler(task_conv_handler)
    
    # task_cmds = get_task_command_handlers(conn)
    # for cmd in task_cmds:
    #     application.add_handler(cmd)
    # We can keep command handlers for shortcut/debugging, but the user requested management via buttons.
    # Let's keep them as alternative access points if they don't conflict. 
    # But get_add_task_handler creates a NEW ConversationHandler which might conflict with the main one.
    # So we should remove `task_conv_handler`.
    
    # APScheduler
    scheduler = AsyncIOScheduler()
    scheduler.start()
    application.scheduler = scheduler
    
    # Load tasks
    load_tasks(scheduler, application.bot, conn)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    database = "data/conversations_data.db"

    conn = create_connection(database)
    create_table(conn)

    main()
