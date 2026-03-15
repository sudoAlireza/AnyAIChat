import os
import glob
import time
import logging
import gettext
import asyncio
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
from database.connection import DatabasePool
from database.database import create_table, get_all_tasks
from config import (
    DATABASE_PATH, TELEGRAM_BOT_TOKEN, LANGUAGE, LOG_LEVEL,
    REMINDER_CHECK_INTERVAL_MINUTES, AUTHORIZED_USER, ALLOW_ALL_USERS,
    TEMP_FILE_MAX_AGE_HOURS,
)
from monitoring.metrics import log_metrics_task
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
    handle_task_plan_approval,
    list_tasks,
    delete_task_handler,
    back_to_time_handler,
    set_scheduler,
    schedule_task_job,
    open_settings_menu,
    open_models_menu,
    set_model_handler,
    open_storage_menu,
    toggle_web_search,
    handle_api_key,
    update_api_key_handler,
    open_persona_menu,
    handle_persona_input,
    open_reminders_menu,
    start_add_reminder,
    handle_reminder_input,
    delete_reminder_handler,
    open_knowledge_menu,
    start_add_knowledge,
    handle_knowledge_input,
    delete_knowledge_handler,
    generate_image_handler,
    check_reminders_task,
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup translation
localedir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "locales")

try:
    gettext.install("messages", localedir, names=("ngettext",))
    gettext.translation("messages", localedir, languages=[LANGUAGE], fallback=True).install()
except Exception as e:
    print(f"Translation setup error: {e}")
    import builtins
    builtins.__dict__['_'] = lambda x: x

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=LOG_LEVEL
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, CONVERSATION, CONVERSATION_HISTORY, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_TIME, TASKS_ADD_INTERVAL, TASKS_CONFIRM_PLAN, SETTINGS_MENU, MODELS_MENU, STORAGE_MENU, API_KEY_INPUT, PERSONA_MENU, PERSONA_INPUT, REMINDERS_MENU, REMINDERS_INPUT, KNOWLEDGE_MENU, KNOWLEDGE_INPUT = range(18)


def _check_access_config():
    """Phase 1.6: Warn at startup if access control is not explicitly configured."""
    if not AUTHORIZED_USER and not ALLOW_ALL_USERS:
        logger.warning(
            "Neither AUTHORIZED_USER nor ALLOW_ALL_USERS=true is set. "
            "The bot will deny all access. Set AUTHORIZED_USER to a comma-separated "
            "list of Telegram user IDs, or set ALLOW_ALL_USERS=true to allow everyone."
        )
    elif ALLOW_ALL_USERS:
        logger.warning("ALLOW_ALL_USERS=true — bot is open to all Telegram users.")


def _cleanup_temp_files():
    """Phase 4.2: Remove stale temp files from data/ directory."""
    data_dir = os.path.abspath("data")
    if not os.path.exists(data_dir):
        return

    now = time.time()
    max_age_seconds = TEMP_FILE_MAX_AGE_HOURS * 3600
    patterns = ["voice_*", "doc_*", "rag_*"]
    removed = 0

    for pattern in patterns:
        for filepath in glob.glob(os.path.join(data_dir, pattern)):
            try:
                if os.path.isfile(filepath) and (now - os.path.getmtime(filepath)) > max_age_seconds:
                    os.remove(filepath)
                    removed += 1
            except OSError as e:
                logger.warning(f"Failed to clean up temp file {filepath}: {e}")

    if removed:
        logger.info(f"Cleaned up {removed} stale temp files")


async def _cleanup_temp_files_async():
    """Async wrapper for temp file cleanup (for APScheduler)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _cleanup_temp_files)


def entry_points():
    return [
        CommandHandler("start", start),
        CallbackQueryHandler(start_over, pattern="^Start_Again"),
    ]

def states():
    return {
        API_KEY_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_api_key),
        ],
        CHOOSING: [
            CallbackQueryHandler(start_conversation, pattern="^New_Conversation$"),
            CallbackQueryHandler(get_conversation_history, pattern="^PAGE#"),
            CallbackQueryHandler(open_tasks_menu, pattern="^Tasks_Menu$"),
            CallbackQueryHandler(open_reminders_menu, pattern="^Reminders_Menu$"),
            CallbackQueryHandler(open_knowledge_menu, pattern="^Knowledge_Menu$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(done, pattern="^End_Conversation$"),
        ],
        CONVERSATION: [
            CommandHandler("image", generate_image_handler),
            MessageHandler((filters.TEXT | filters.VOICE | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, reply_and_new_message),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        CONVERSATION_HISTORY: [
            CallbackQueryHandler(start_conversation, pattern="^New_Conversation$"),
            CallbackQueryHandler(get_conversation_history, pattern="^PAGE#"),
            MessageHandler(filters.Regex("^/conv"), get_conversation_handler),
            CallbackQueryHandler(delete_conversation_handler, pattern="^Delete_Conversation$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        TASKS_MENU: [
            CallbackQueryHandler(start_add_task, pattern="^Tasks_Add$"),
            CallbackQueryHandler(list_tasks, pattern="^Tasks_List$"),
            CallbackQueryHandler(delete_task_handler, pattern="^TASK_DELETE#"),
            CallbackQueryHandler(open_tasks_menu, pattern="^Tasks_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        TASKS_ADD_PROMPT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_prompt),
            CallbackQueryHandler(open_tasks_menu, pattern="^Tasks_Menu$"),
        ],
        TASKS_ADD_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_time),
            CallbackQueryHandler(start_add_task, pattern="^Tasks_Add$"),
        ],
        TASKS_ADD_INTERVAL: [
            CallbackQueryHandler(handle_task_interval, pattern="^Tasks_Interval_"),
            CallbackQueryHandler(handle_task_prompt, pattern="^Back_To_Prompt$"),
        ],
        TASKS_CONFIRM_PLAN: [
            CallbackQueryHandler(handle_task_plan_approval, pattern="^Plan_"),
            CallbackQueryHandler(back_to_time_handler, pattern="^Back_To_Time$"),
        ],
        SETTINGS_MENU: [
            CallbackQueryHandler(open_models_menu, pattern="^open_models_menu$"),
            CallbackQueryHandler(open_persona_menu, pattern="^Persona_Menu$"),
            CallbackQueryHandler(open_storage_menu, pattern="^Storage_Menu$"),
            CallbackQueryHandler(toggle_web_search, pattern="^TOGGLE_WEB_SEARCH$"),
            CallbackQueryHandler(update_api_key_handler, pattern="^UPDATE_API_KEY$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        PERSONA_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_persona_input),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
        ],
        REMINDERS_MENU: [
            CallbackQueryHandler(start_add_reminder, pattern="^Add_Reminder$"),
            CallbackQueryHandler(delete_reminder_handler, pattern="^REMINDER_DELETE#"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
            CallbackQueryHandler(open_reminders_menu, pattern="^Reminders_Menu$"),
        ],
        REMINDERS_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reminder_input),
            CallbackQueryHandler(open_reminders_menu, pattern="^Reminders_Menu$"),
        ],
        KNOWLEDGE_MENU: [
            CallbackQueryHandler(start_add_knowledge, pattern="^Add_Knowledge$"),
            CallbackQueryHandler(delete_knowledge_handler, pattern="^KNOWLEDGE_DELETE#"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
            CallbackQueryHandler(open_knowledge_menu, pattern="^Knowledge_Menu$"),
        ],
        KNOWLEDGE_INPUT: [
            MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_knowledge_input),
            CallbackQueryHandler(open_knowledge_menu, pattern="^Knowledge_Menu$"),
        ],
        MODELS_MENU: [
            CallbackQueryHandler(set_model_handler, pattern="^SET_MODEL_"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        STORAGE_MENU: [
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
    }

def fallbacks():
    return [
        CommandHandler("start", start),
        CallbackQueryHandler(start_over, pattern="^Start_Again"),
    ]


async def post_init(application: Application):
    """Initialize async resources after the application starts."""
    os.makedirs("data", exist_ok=True)

    # Initialize async database pool
    pool = DatabasePool(DATABASE_PATH)
    await create_table(pool)
    application.bot_data["db_pool"] = pool
    logger.info(f"Database pool initialized at {DATABASE_PATH}")

    # Phase 4.2: Cleanup stale temp files at startup
    _cleanup_temp_files()

    # Load and schedule existing tasks
    try:
        all_tasks = []
        offset = 0
        while True:
            batch = await get_all_tasks(pool, batch_size=100, offset=offset)
            if not batch:
                break
            all_tasks.extend(batch)
            offset += 100

        for task in all_tasks:
            schedule_task_job(
                task_id=task["id"],
                user_id=task["user_id"],
                prompt=task["prompt"],
                run_time=task["run_time"],
                interval=task["interval"],
                plan_json=task["plan_json"],
                start_date=task["start_date"],
            )
        logger.info(f"Loaded {len(all_tasks)} active tasks.")
    except Exception as e:
        logger.error(f"Failed to load tasks: {e}")


async def post_shutdown(application: Application):
    """Phase 5.5: Graceful shutdown — clean up resources."""
    logger.info("Shutting down...")

    # Close database pool
    pool = application.bot_data.get("db_pool")
    if pool:
        await pool.close_all()
        logger.info("Database pool closed")

    # Clean up temp files
    _cleanup_temp_files()

    logger.info("Shutdown complete")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables.")
        return

    # Phase 1.6: Check access configuration
    _check_access_config()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=entry_points(),
        states=states(),
        fallbacks=fallbacks(),
        name="gemini_conversation",
        persistent=False,
        allow_reentry=True,
    )
    application.add_handler(conv_handler)

    scheduler = AsyncIOScheduler()
    set_scheduler(scheduler, application)

    # Schedule recurring jobs
    scheduler.add_job(check_reminders_task, 'interval', minutes=REMINDER_CHECK_INTERVAL_MINUTES)
    scheduler.add_job(_cleanup_temp_files_async, 'interval', hours=1)
    scheduler.add_job(log_metrics_task, 'interval', minutes=5)

    scheduler.start()
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
