import logging
import os
import datetime
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, JobQueue
from database.database import add_task, get_user_tasks, delete_task, get_all_tasks
from core import GeminiChat
from functools import wraps

logger = logging.getLogger(__name__)

# Stages for Add Task Conversation
ASK_PROMPT, ASK_TIME, ASK_INTERVAL = range(3)

def restricted(func):
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        authorized_users = [int(user_id.strip()) for user_id in os.getenv("AUTHORIZED_USER", "").split(',')]
        if user_id not in authorized_users:
            logger.info(f"Unauthorized access denied for {user_id}.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

async def execute_task(context: ContextTypes.DEFAULT_TYPE):
    """Job to execute a scheduled task."""
    job = context.job
    task_data = job.data
    chat_id = task_data['chat_id']
    prompt = task_data['prompt']
    user_id = task_data['user_id']
    
    logger.info(f"Executing task for user {user_id} in chat {chat_id}: {prompt}")
    
    try:
        gemini_chat = GeminiChat(gemini_token=os.getenv("GEMINI_API_TOKEN"))
        gemini_chat.start_chat()
        response = gemini_chat.send_message(prompt)
        gemini_chat.close()
        
        await context.bot.send_message(chat_id=chat_id, text=f"🔔 *Scheduled Task Execution*\n\n**Prompt:** {prompt}\n\n**Response:**\n{response}", parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Failed to execute task: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Failed to execute scheduled task '{prompt}': {e}")


@restricted
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please enter the prompt you want to send to Gemini.")
    return ASK_PROMPT

@restricted
async def receive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['task_prompt'] = update.message.text
    await update.message.reply_text(
        "Saved. Now, when should this task start? (Format: YYYY-MM-DD HH:MM or HH:MM for today/tomorrow)\n"
        "Example: 2023-10-27 15:30 or 15:30"
    )
    return ASK_TIME

@restricted
async def receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text
    try:
        if len(time_str) == 5: # HH:MM
             now = datetime.datetime.now()
             t = datetime.datetime.strptime(time_str, "%H:%M").time()
             start_time = datetime.datetime.combine(now.date(), t)
             if start_time < now:
                 start_time += datetime.timedelta(days=1)
        else:
             start_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M")
             
        context.user_data['task_start_time'] = start_time
        await update.message.reply_text(
            "Got it. Enter the interval in seconds (0 for one-time):"
        )
        return ASK_INTERVAL
    except ValueError:
        await update.message.reply_text("Invalid format. Please try again (YYYY-MM-DD HH:MM or HH:MM).")
        return ASK_TIME

@restricted
async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> int:
    try:
        interval = int(update.message.text)
        prompt = context.user_data['task_prompt']
        start_time = context.user_data['task_start_time']
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Save to DB
        task_id = add_task(conn, (user_id, chat_id, prompt, start_time.strftime("%Y-%m-%d %H:%M:%S"), interval))
        
        # Schedule Job
        job_queue = context.job_queue
        # Calculate delay
        now = datetime.datetime.now()
        delay = (start_time - now).total_seconds()
        
        if delay < 0:
             delay = 0 # Run immediately if past
             
        data = {'chat_id': chat_id, 'prompt': prompt, 'user_id': user_id, 'task_id': task_id, 'interval': interval}
        
        if interval > 0:
            job_queue.run_repeating(execute_task, interval=interval, first=delay, data=data, name=str(task_id))
        else:
            job_queue.run_once(execute_task, when=delay, data=data, name=str(task_id))
            
        await update.message.reply_text(f"Task scheduled!\nID: {task_id}\nPrompt: {prompt}\nStart: {start_time}\nInterval: {interval}s")
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("Invalid number. Please enter an integer for seconds.")
        return ASK_INTERVAL

@restricted
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Task creation cancelled.")
    return ConversationHandler.END

@restricted
async def list_tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> None:
    user_id = update.effective_user.id
    tasks = get_user_tasks(conn, user_id)
    if not tasks:
        await update.message.reply_text("No scheduled tasks found.")
        return
        
    message = "*Your Scheduled Tasks:*\n\n"
    for task in tasks:
        message += f"🆔 `{task['id']}`\n"
        message += f"📝 Prompt: {task['prompt']}\n"
        message += f"🕒 Start: {task['start_time']}\n"
        message += f"🔄 Interval: {task['interval_seconds']}s\n"
        message += "-------------------------\n"
        
    await update.message.reply_text(message, parse_mode="Markdown")

@restricted
async def delete_task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, conn) -> None:
    try:
        # Check if args are passed
        if not context.args:
            await update.message.reply_text("Usage: /deletetask <task_id>")
            return
            
        task_id = int(context.args[0])
        
        # Check if task exists and belongs to user (simple check)
        tasks = get_user_tasks(conn, update.effective_user.id)
        task_ids = [t['id'] for t in tasks]
        
        if task_id not in task_ids:
             await update.message.reply_text("Task not found or not owned by you.")
             return

        delete_task(conn, task_id)
        
        # Remove from JobQueue
        current_jobs = context.job_queue.get_jobs_by_name(str(task_id))
        if current_jobs:
            for job in current_jobs:
                job.schedule_removal()
            await update.message.reply_text(f"Task {task_id} deleted and unscheduled.")
        else:
            await update.message.reply_text(f"Task {task_id} deleted from DB (was not running).")
        
    except ValueError:
         await update.message.reply_text("Invalid task ID.")
    except Exception as e:
        logger.error(f"Error deleting task: {e}")
        await update.message.reply_text("Error deleting task.")

def load_tasks(application, conn):
    """Load tasks from DB and schedule them on startup."""
    tasks = get_all_tasks(conn)
    count = 0
    for task in tasks:
        try:
            task_id = task['id']
            chat_id = task['chat_id']
            user_id = task['user_id']
            prompt = task['prompt']
            start_time_str = task['start_time']
            interval = task['interval_seconds']
            
            start_time = datetime.datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")
            now = datetime.datetime.now()
            
            data = {'chat_id': chat_id, 'prompt': prompt, 'user_id': user_id, 'task_id': task_id, 'interval': interval}
            
            # If start time is past
            delay = (start_time - now).total_seconds()
            if delay < 0:
                delay = 0 
                # If it was a one-time task in past, maybe don't schedule? 
                # Or schedule immediately?
                # If interval > 0, we should align strictly or just start now?
                # For simplicity, if repeating, start now. If one-time in past, ignore or run now?
                # Let's run now for simplicity.
                if interval == 0:
                     # One time task in past. Skip it? or run it?
                     # Let's skip to avoid spam on restart if it was long ago?
                     # actually user might miss it. Let's run it.
                     pass 

            if interval > 0:
                application.job_queue.run_repeating(execute_task, interval=interval, first=delay, data=data, name=str(task_id))
            else:
                # If one time and in past, maybe we should check if it was already run?
                # We don't have status in DB. 
                # For now, schedule it.
                 application.job_queue.run_once(execute_task, when=delay, data=data, name=str(task_id))
            
            count += 1
        except Exception as e:
            logger.error(f"Failed to load task {task.get('id')}: {e}")
            
    logger.info(f"Loaded {count} tasks from database.")

def get_add_task_handler(conn):
    return ConversationHandler(
        entry_points=[CommandHandler("addtask", add_task_start)],
        states={
            ASK_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prompt)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time)],
            ASK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: receive_interval(u, c, conn))],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

def get_task_command_handlers(conn):
    return [
        CommandHandler("mytasks", lambda u, c: list_tasks_handler(u, c, conn)),
        CommandHandler("deletetask", lambda u, c: delete_task_handler(u, c, conn)),
    ]
