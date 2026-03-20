import os
import glob
import time
import logging
import gettext
import asyncio
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    InlineQueryHandler,
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
    handle_task_days,
    back_to_days_handler,
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
    show_all_models_handler,
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
    # Feature handlers (batch 1)
    search_menu_handler,
    handle_search_input,
    browse_tags_handler,
    tag_browse_results_handler,
    export_conversation_handler,
    share_conversation_handler,
    tag_conversation_handler,
    handle_tag_input,
    remove_tag_handler,
    usage_dashboard_handler,
    open_shortcuts_menu,
    start_add_shortcut,
    handle_shortcut_input,
    delete_shortcut_handler,
    open_pinned_context_menu,
    handle_pinned_context_input,
    clear_pinned_context_handler,
    language_menu_handler,
    set_language_handler,
    inline_query_handler,
    weekly_summary_task,
    # Feature handlers (batch 2)
    templates_menu_handler,
    select_template_handler,
    translation_mode_handler,
    start_translation_handler,
    bookmarks_menu_handler,
    delete_bookmark_handler,
    bookmark_message_handler,
    prompt_library_handler,
    start_add_prompt_handler,
    handle_prompt_add,
    use_prompt_handler,
    delete_prompt_handler,
    suggest_followup_handler,
    voice_output_handler,
    feedback_up_handler,
    feedback_down_handler,
    branch_conversation_handler,
    set_resume_point_handler,
    briefing_menu_handler,
    handle_briefing_time_input,
    disable_briefing_handler,
    url_monitor_menu_handler,
    start_add_url_monitor,
    handle_url_monitor_input,
    delete_url_monitor_handler,
    check_url_monitors_task,
    daily_briefing_task,
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

class UserIdFilter(logging.Filter):
    """Inject user_id into every log record from the current async context."""
    def filter(self, record):
        from bot.conversation_handlers import _current_user_id
        record.user_id = _current_user_id.get('-')
        return True

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - [user:%(user_id)s] %(message)s", level=LOG_LEVEL
)
logging.root.addFilter(UserIdFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, CONVERSATION, CONVERSATION_HISTORY, TASKS_MENU, TASKS_ADD_PROMPT, TASKS_ADD_DAYS, TASKS_ADD_TIME, TASKS_ADD_INTERVAL, TASKS_CONFIRM_PLAN, SETTINGS_MENU, MODELS_MENU, STORAGE_MENU, API_KEY_INPUT, PERSONA_MENU, PERSONA_INPUT, REMINDERS_MENU, REMINDERS_INPUT, KNOWLEDGE_MENU, KNOWLEDGE_INPUT, SEARCH_INPUT, SHORTCUTS_MENU, SHORTCUTS_INPUT, TAGS_INPUT, PINNED_CONTEXT_INPUT, TEMPLATES_MENU, BOOKMARKS_MENU, PROMPT_LIBRARY, PROMPT_ADD, BRIEFING_MENU, URL_MONITOR_MENU, URL_MONITOR_INPUT = range(31)


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
            CallbackQueryHandler(search_menu_handler, pattern="^Search_Menu$"),
            CallbackQueryHandler(bookmarks_menu_handler, pattern="^Bookmarks_Menu$"),
            CallbackQueryHandler(prompt_library_handler, pattern="^Prompt_Library$"),
            CallbackQueryHandler(usage_dashboard_handler, pattern="^Usage_Dashboard$"),
            CallbackQueryHandler(templates_menu_handler, pattern="^Templates_Menu$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(done, pattern="^End_Conversation$"),
        ],
        CONVERSATION: [
            CommandHandler("image", generate_image_handler),
            CallbackQueryHandler(suggest_followup_handler, pattern="^Suggest_Followup$"),
            CallbackQueryHandler(voice_output_handler, pattern="^Voice_Output$"),
            CallbackQueryHandler(bookmark_message_handler, pattern="^Bookmark_Msg$"),
            CallbackQueryHandler(feedback_up_handler, pattern="^Feedback_Up$"),
            CallbackQueryHandler(feedback_down_handler, pattern="^Feedback_Down$"),
            MessageHandler((filters.TEXT | filters.VOICE | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, reply_and_new_message),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        CONVERSATION_HISTORY: [
            CallbackQueryHandler(start_conversation, pattern="^New_Conversation$"),
            CallbackQueryHandler(get_conversation_history, pattern="^PAGE#"),
            CallbackQueryHandler(lambda update, ctx: update.callback_query.answer(), pattern="^noop$"),
            CallbackQueryHandler(get_conversation_handler, pattern="^CONV_SELECT#"),
            MessageHandler(filters.Regex("^/conv"), get_conversation_handler),
            CallbackQueryHandler(delete_conversation_handler, pattern="^Delete_Conversation$"),
            CallbackQueryHandler(export_conversation_handler, pattern="^Export_Conversation$"),
            CallbackQueryHandler(share_conversation_handler, pattern="^Share_Conversation$"),
            CallbackQueryHandler(tag_conversation_handler, pattern="^Tag_Conversation$"),
            CallbackQueryHandler(branch_conversation_handler, pattern="^Branch_Conversation$"),
            CallbackQueryHandler(set_resume_point_handler, pattern="^Set_Resume_Point$"),
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
        TASKS_ADD_DAYS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_days),
            CallbackQueryHandler(open_tasks_menu, pattern="^Tasks_Menu$"),
        ],
        TASKS_ADD_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_time),
            CallbackQueryHandler(back_to_days_handler, pattern="^Back_To_Days$"),
        ],
        TASKS_ADD_INTERVAL: [
            CallbackQueryHandler(handle_task_interval, pattern="^Tasks_Interval_"),
            CallbackQueryHandler(back_to_days_handler, pattern="^Back_To_Days$"),
        ],
        TASKS_CONFIRM_PLAN: [
            CallbackQueryHandler(handle_task_plan_approval, pattern="^Plan_"),
            CallbackQueryHandler(back_to_time_handler, pattern="^Back_To_Time$"),
        ],
        SETTINGS_MENU: [
            CallbackQueryHandler(open_models_menu, pattern="^open_models_menu$"),
            CallbackQueryHandler(open_persona_menu, pattern="^Persona_Menu$"),
            CallbackQueryHandler(open_pinned_context_menu, pattern="^Pinned_Context_Menu$"),
            CallbackQueryHandler(open_shortcuts_menu, pattern="^Shortcuts_Menu$"),
            CallbackQueryHandler(open_storage_menu, pattern="^Storage_Menu$"),
            CallbackQueryHandler(toggle_web_search, pattern="^TOGGLE_WEB_SEARCH$"),
            CallbackQueryHandler(language_menu_handler, pattern="^Language_Menu$"),
            CallbackQueryHandler(briefing_menu_handler, pattern="^Briefing_Menu$"),
            CallbackQueryHandler(url_monitor_menu_handler, pattern="^URL_Monitor_Menu$"),
            CallbackQueryHandler(update_api_key_handler, pattern="^UPDATE_API_KEY$"),
            CallbackQueryHandler(set_language_handler, pattern="^SET_LANG_"),
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
            CallbackQueryHandler(show_all_models_handler, pattern="^Show_All_Models$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        STORAGE_MENU: [
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        SEARCH_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_input),
            CallbackQueryHandler(browse_tags_handler, pattern="^Browse_Tags$"),
            CallbackQueryHandler(tag_browse_results_handler, pattern="^TAG_BROWSE#"),
            CallbackQueryHandler(get_conversation_handler, pattern="^CONV_SELECT#"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        SHORTCUTS_MENU: [
            CallbackQueryHandler(start_add_shortcut, pattern="^Add_Shortcut$"),
            CallbackQueryHandler(delete_shortcut_handler, pattern="^SHORTCUT_DELETE#"),
            CallbackQueryHandler(open_shortcuts_menu, pattern="^Shortcuts_Menu$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
        ],
        SHORTCUTS_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_shortcut_input),
            CallbackQueryHandler(open_shortcuts_menu, pattern="^Shortcuts_Menu$"),
        ],
        TAGS_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tag_input),
            CallbackQueryHandler(remove_tag_handler, pattern="^TAG_REMOVE#"),
            CallbackQueryHandler(get_conversation_handler, pattern="^CONV_SELECT#"),
        ],
        PINNED_CONTEXT_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pinned_context_input),
            CallbackQueryHandler(clear_pinned_context_handler, pattern="^Clear_Pinned_Context$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
        ],
        TEMPLATES_MENU: [
            CallbackQueryHandler(select_template_handler, pattern="^TEMPLATE#"),
            CallbackQueryHandler(translation_mode_handler, pattern="^Translation_Mode$"),
            CallbackQueryHandler(start_translation_handler, pattern="^TRANSLATE_TO#"),
            CallbackQueryHandler(templates_menu_handler, pattern="^Templates_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        BOOKMARKS_MENU: [
            CallbackQueryHandler(delete_bookmark_handler, pattern="^BOOKMARK_DELETE#"),
            CallbackQueryHandler(bookmarks_menu_handler, pattern="^Bookmarks_Menu$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        PROMPT_LIBRARY: [
            CallbackQueryHandler(start_add_prompt_handler, pattern="^Add_Prompt$"),
            CallbackQueryHandler(use_prompt_handler, pattern="^USE_PROMPT#"),
            CallbackQueryHandler(delete_prompt_handler, pattern="^PROMPT_DELETE#"),
            CallbackQueryHandler(prompt_library_handler, pattern="^Prompt_Library$"),
            CallbackQueryHandler(start_over, pattern="^Start_Again"),
        ],
        PROMPT_ADD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt_add),
            CallbackQueryHandler(prompt_library_handler, pattern="^Prompt_Library$"),
        ],
        BRIEFING_MENU: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_briefing_time_input),
            CallbackQueryHandler(disable_briefing_handler, pattern="^Disable_Briefing$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
        ],
        URL_MONITOR_MENU: [
            CallbackQueryHandler(start_add_url_monitor, pattern="^Add_URL_Monitor$"),
            CallbackQueryHandler(delete_url_monitor_handler, pattern="^MONITOR_DELETE#"),
            CallbackQueryHandler(url_monitor_menu_handler, pattern="^URL_Monitor_Menu$"),
            CallbackQueryHandler(open_settings_menu, pattern="^Settings_Menu$"),
        ],
        URL_MONITOR_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_monitor_input),
            CallbackQueryHandler(url_monitor_menu_handler, pattern="^URL_Monitor_Menu$"),
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

    # Register bot commands with Telegram
    await application.bot.set_my_commands([
        BotCommand("start", "Open main menu"),
        BotCommand("image", "Generate an image (use in conversation)"),
    ])

    # Store bot username for deep-link URLs
    bot_info = await application.bot.get_me()
    application.bot_data["bot_username"] = bot_info.username

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

    # Inline mode handler (outside ConversationHandler)
    application.add_handler(InlineQueryHandler(inline_query_handler))

    scheduler = AsyncIOScheduler()
    set_scheduler(scheduler, application)

    # Schedule recurring jobs
    scheduler.add_job(check_reminders_task, 'interval', minutes=REMINDER_CHECK_INTERVAL_MINUTES)
    scheduler.add_job(_cleanup_temp_files_async, 'interval', hours=1)
    scheduler.add_job(log_metrics_task, 'interval', minutes=5)
    scheduler.add_job(weekly_summary_task, 'cron', day_of_week='sun', hour=10, minute=0)
    scheduler.add_job(check_url_monitors_task, 'interval', minutes=30)
    scheduler.add_job(daily_briefing_task, 'cron', minute='*')  # Check every minute for matching briefing times

    scheduler.start()
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
