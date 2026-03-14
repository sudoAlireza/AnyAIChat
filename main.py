import os
import logging
import gettext
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database.database import create_connection, create_table, get_all_tasks
from bot.conversation_handlers import (
    start,
    start_over,
    start_conversation,
    reply_and_new_message,
    get_conversation_history,
    delete_conversation_handler,
    get_conversation_handler,
    done,
    open_tasks_menu,
    start_add_task,
    handle_task_prompt,
    handle_task_time,
    handle_task_interval,
    list_tasks,
    delete_task_handler,
    set_scheduler,
    schedule_task_job,
    open_settings_menu,
    open_models_menu,
    set_model_handler,
    open_storage_menu,
    toggle_web_search,
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup translation
localedir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "locales")
lang = os.getenv("LANGUAGE", "en")
log_level = os.getenv("LOG_LEVEL", "INFO").upper()

try:
    gettext.install("messages", localedir, names=("ngettext",))
    gettext.translation("messages", localedir, languages=[lang], fallback=True).install()
except Exception as e:
    print(f"Translation setup error: {e}")
    import builtins
    builtins.__dict__['_'] = lambda x: x

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=log_level
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, CONVERSATION, CONVERSATION_HISTORY, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_TIME, TASKS_ADD_INTERVAL, SETTINGS_MENU, MODELS_MENU, STORAGE_MENU = range(10)

# Global connection
database_path = "data/conversations_data.db"
os.makedirs("data", exist_ok=True)
conn = create_connection(database_path)
create_table(conn)

def entry_points():
    return [
        CommandHandler("start", start),
        CallbackQueryHandler(
            lambda update, context: start_over(update, context, conn),
            pattern="^Start_Again",
        ),
    ]

def states():
    return {
        CHOOSING: [
            CallbackQueryHandler(start_conversation, pattern="^New_Conversation$"),
            CallbackQueryHandler(
                lambda update, context: get_conversation_history(update, context, conn),
                pattern="^PAGE#",
            ),
            CallbackQueryHandler(open_tasks_menu, pattern="^Tasks_Menu$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(done, pattern="^End_Conversation$"),
        ],
        CONVERSATION: [
            MessageHandler((filters.TEXT | filters.VOICE | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, reply_and_new_message),
            CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
        ],
        CONVERSATION_HISTORY: [
            CallbackQueryHandler(
                lambda update, context: get_conversation_history(update, context, conn),
                pattern="^PAGE#",
            ),
            MessageHandler(filters.Regex("^/conv"), lambda update, context: get_conversation_handler(update, context, conn)),
            CallbackQueryHandler(
                lambda update, context: delete_conversation_handler(update, context, conn),
                pattern="^Delete_Conversation$",
            ),
            CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
        ],
        TASKS_MENU: [
            CallbackQueryHandler(start_add_task, pattern="^Tasks_Add$"),
            CallbackQueryHandler(lambda update, context: list_tasks(update, context, conn), pattern="^Tasks_List$"),
            CallbackQueryHandler(lambda update, context: delete_task_handler(update, context, conn), pattern="^TASK_DELETE#"),
            CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
        ],
        TASKS_ADD_PROMPT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_prompt),
        ],
        TASKS_ADD_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_time),
        ],
        TASKS_ADD_INTERVAL: [
            CallbackQueryHandler(lambda update, context: handle_task_interval(update, context, conn), pattern="^Tasks_Interval_"),
        ],
        SETTINGS_MENU: [
            CallbackQueryHandler(open_models_menu, pattern="^open_models_menu$"),
            CallbackQueryHandler(open_storage_menu, pattern="^Storage_Menu$"),
            CallbackQueryHandler(toggle_web_search, pattern="^TOGGLE_WEB_SEARCH$"),
            CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
        ],
        MODELS_MENU: [
            CallbackQueryHandler(set_model_handler, pattern="^SET_MODEL_"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
        ],
        STORAGE_MENU: [
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
        ],
    }

def fallbacks():
    return [
        CommandHandler("start", start),
        CallbackQueryHandler(lambda update, context: start_over(update, context, conn), pattern="^Start_Again"),
    ]

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables.")
        return

    application = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=entry_points(),
        states=states(),
        fallbacks=fallbacks(),
        name="gemini_conversation",
        persistent=False, # Changed to False since we use SQLite now
        allow_reentry=True,
    )
    application.add_handler(conv_handler)

    scheduler = AsyncIOScheduler()
    set_scheduler(scheduler, application)

    try:
        tasks = get_all_tasks(conn)
        for task in tasks:
            schedule_task_job(
                task_id=task["id"],
                user_id=task["user_id"],
                prompt=task["prompt"],
                run_time=task["run_time"],
                interval=task["interval"],
            )
        logger.info(f"Loaded {len(tasks)} tasks.")
    except Exception as e:
        logger.error(f"Failed to load tasks: {e}")

    scheduler.start()
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
