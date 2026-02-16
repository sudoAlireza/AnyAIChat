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

async def post_init(application: Application) -> None:
    # We need to access conn here. But post_init only gets application.
    # We can attach conn to application.bot_data or similar if needed, or just use global/closure.
    # Since main() has conn, we can pass it if we define post_init inside main or pass it.
    # However, ApplicationBuilder.post_init takes a coroutine.
    # Let's try to load tasks after build but before run_polling if possible, OR use a job_queue.run_once(0).
    # actually run_polling blocks.
    # We can use application.job_queue.run_once(lambda c: load_tasks_from_db(application, conn), 0)
    # But connecting to DB inside main and passing to imported function is fine.
    # Let's just call load_tasks(application, conn) before run_polling.
    # Wait, load_tasks is async? Yes.
    # We can't await it in sync main. 
    # Application.run_polling takes post_init argument.
    pass

def main() -> None:
    persistence = PicklePersistence(filepath="conversation_persistence")
    application = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).persistence(persistence).build()

    conv_handler = create_conv_handler()
    application.add_handler(conv_handler)
    
    # Task Handlers
    task_conv_handler = get_add_task_handler(conn)
    application.add_handler(task_conv_handler)
    
    task_cmds = get_task_command_handlers(conn)
    for cmd in task_cmds:
        application.add_handler(cmd)

    # Load tasks
    # application.job_queue is available.
    # We need to run load_tasks(application, conn).
    # Since we are in sync main, we can schedule a job to run immediately.
    async def load_tasks_callback(context):
        await load_tasks(application, conn)
        
    application.job_queue.run_once(load_tasks_callback, 0)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    database = "data/conversations_data.db"

    conn = create_connection(database)
    create_table(conn)

    main()
