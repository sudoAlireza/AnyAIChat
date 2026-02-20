import os
import logging
import gettext
from typing import Dict, List
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database.database import create_connection, create_table, get_all_tasks
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
    open_tasks_menu,
    start_add_task,
    handle_task_prompt,
    handle_task_time,
    handle_task_interval,
    list_tasks,
    delete_task_handler,
    set_scheduler,
    schedule_task_job,
    set_db_connection,
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

CHOOSING, IMAGE_CHOICE, CONVERSATION, CONVERSATION_HISTORY, IMAGE_CONVERSATION, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_TIME, TASKS_ADD_INTERVAL = range(
    9
)

# Global database connection (set at startup)
_db_conn = None


def get_db_connection():
    """Get the global database connection"""
    return _db_conn


def entry_points() -> List:
    """Define conversation entry points"""
    return [
        CommandHandler("start", start),
        CallbackQueryHandler(
            lambda update, context: start_over(update, context, get_db_connection()),
            pattern="^Start_Again",
        ),
    ]


def states() -> Dict:
    """Define conversation states and their handlers"""
    return {
        CHOOSING: [
            CallbackQueryHandler(
                start_conversation,
                pattern="^New_Conversation$",
            ),
            CallbackQueryHandler(
                start_image_conversation,
                pattern="^Image_Description$",
            ),
            CallbackQueryHandler(
                lambda update, context: get_conversation_history(update, context, get_db_connection()),
                pattern="^PAGE#",
            ),
            CallbackQueryHandler(
                done,
                pattern="^End_Conversation$",
            ),
            CallbackQueryHandler(
                open_tasks_menu,
                pattern="^Tasks_Menu$",
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
                lambda update, context: get_conversation_history(update, context, get_db_connection()),
                pattern="^PAGE#",
            ),
            MessageHandler(
                filters.Regex("^/conv"),
                lambda update, context: get_conversation_handler(update, context, get_db_connection()),
            ),
            CallbackQueryHandler(
                lambda update, context: delete_conversation_handler(update, context, get_db_connection()),
                pattern="^Delete_Conversation$",
            ),
        ],
        IMAGE_CONVERSATION: [
            MessageHandler(
                filters.TEXT & ~filters.Regex("^/"),
                reply_to_image_conversation,
            )
        ],
        TASKS_MENU: [
            CallbackQueryHandler(
                start_add_task,
                pattern="^Tasks_Add$",
            ),
            CallbackQueryHandler(
                lambda update, context: list_tasks(update, context, get_db_connection()),
                pattern="^Tasks_List$",
            ),
            CallbackQueryHandler(
                lambda update, context: delete_task_handler(update, context, get_db_connection()),
                pattern="^TASK_DELETE#",
            ),
            CallbackQueryHandler(
                open_tasks_menu,
                pattern="^Tasks_Menu$",
            ),
        ],
        TASKS_ADD_PROMPT: [
            MessageHandler(
                filters.TEXT & ~filters.Regex("^/"),
                handle_task_prompt,
            )
        ],
        TASKS_ADD_TIME: [
            MessageHandler(
                filters.TEXT & ~filters.Regex("^/"),
                handle_task_time,
            )
        ],
        TASKS_ADD_INTERVAL: [
            CallbackQueryHandler(
                lambda update, context: handle_task_interval(update, context, get_db_connection()),
                pattern="^Tasks_Interval_",
            )
        ],
    }


def fallbacks() -> List:
    """Define conversation fallback handlers"""
    return [
        CallbackQueryHandler(done, pattern="^Done$"),
        CallbackQueryHandler(
            lambda update, context: start_over(update, context, get_db_connection()),
            pattern="^Start_Again",
        ),
    ]


def create_conv_handler() -> ConversationHandler:
    """Create and configure the conversation handler"""
    return ConversationHandler(
        entry_points=entry_points(),
        states=states(),
        fallbacks=fallbacks(),
        persistent=True,
        name="gemini_conversation",
        per_message=False,
        allow_reentry=True,
    )


def main() -> None:
    """Main function to initialize and run the bot"""
    global _db_conn
    
    # Validate environment variables
    required_env_vars = ["TELEGRAM_BOT_TOKEN", "GEMINI_API_TOKEN", "AUTHORIZED_USER"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please check your .env file. See .env.example for reference.")
        return
    
    # Initialize database
    database = "data/conversations_data.db"
    _db_conn = create_connection(database)
    
    if not _db_conn:
        logger.error("Failed to create database connection. Exiting.")
        return
    
    create_table(_db_conn)
    logger.info("Database initialized successfully")
    
    # Set database connection for handlers
    set_db_connection(_db_conn)
    
    # Initialize bot application
    persistence = PicklePersistence(filepath="data/conversation_persistence")
    application = (
        Application.builder()
        .token(os.getenv("TELEGRAM_BOT_TOKEN"))
        .persistence(persistence)
        .build()
    )

    conv_handler = create_conv_handler()
    application.add_handler(conv_handler)

    # Setup APScheduler for recurring tasks and load existing tasks
    scheduler = AsyncIOScheduler()
    set_scheduler(scheduler, application)

    try:
        tasks = get_all_tasks(_db_conn)
        logger.info(f"Loading {len(tasks)} existing tasks from database")
        
        for task in tasks:
            schedule_task_job(
                task_id=task["id"],
                user_id=task["user_id"],
                prompt=task["prompt"],
                run_time=task["run_time"],
                interval=task["interval"],
            )
        
        logger.info(f"Successfully scheduled {len(tasks)} tasks")
    except Exception as e:
        logger.error(f"Failed to load existing tasks for scheduling: {e}")

    scheduler.start()
    logger.info("Scheduler started successfully")
    
    logger.info("Bot is starting... Press Ctrl+C to stop")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
