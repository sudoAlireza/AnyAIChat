import json
import logging
from datetime import datetime
from database.connection import DatabasePool
from security.crypto import encrypt_api_key, decrypt_api_key

logger = logging.getLogger(__name__)

# --- Schema version & migrations ---
# To add a new migration: append a function to MIGRATIONS list.
# Each migration receives an aiosqlite connection. They run in order,
# and the current version is tracked in the `schema_version` table.

MIGRATIONS = []


def migration(func):
    """Decorator to register a migration function."""
    MIGRATIONS.append(func)
    return func


@migration
async def m001_initial_tables(conn):
    """v1: Create initial tables."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            api_key TEXT,
            model_name TEXT,
            grounding INTEGER DEFAULT 0,
            system_instruction TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_base (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_id TEXT NOT NULL,
            content_preview TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reminder_text TEXT NOT NULL,
            remind_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            conv_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            history TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            run_time TEXT NOT NULL,
            interval TEXT NOT NULL,
            plan_json TEXT,
            start_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)


@migration
async def m002_tasks_status_column(conn):
    """v2: Add status column to tasks table."""
    try:
        await conn.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'active'")
    except Exception:
        pass  # Column already exists


@migration
async def m003_add_indexes(conn):
    """v3: Add indexes on lookup columns."""
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_base_user_id ON knowledge_base(user_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_status_remind_at ON reminders(status, remind_at);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_id ON reminders(user_id);")


async def _get_schema_version(conn) -> int:
    """Get current schema version, creating the tracking table if needed."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
    """)
    cursor = await conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def _set_schema_version(conn, version: int):
    """Update the stored schema version."""
    await conn.execute("DELETE FROM schema_version")
    await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


async def create_table(pool: DatabasePool):
    """Run all pending migrations to bring the database schema up to date."""
    conn = await pool.get_connection()
    try:
        current_version = await _get_schema_version(conn)
        target_version = len(MIGRATIONS)

        if current_version >= target_version:
            logger.info(f"Database schema is up to date (v{current_version})")
            return

        logger.info(f"Running migrations: v{current_version} -> v{target_version}")

        for i in range(current_version, target_version):
            migration_func = MIGRATIONS[i]
            logger.info(f"  Running migration {i + 1}: {migration_func.__doc__ or migration_func.__name__}")
            await migration_func(conn)

        await _set_schema_version(conn, target_version)
        await conn.commit()
        logger.info(f"Database schema migrated to v{target_version}")
    except Exception as e:
        logger.error(f"Database migration error: {e}", exc_info=True)
    finally:
        await pool.release_connection(conn)


# --- Conversation Functions ---

async def create_conversation(pool: DatabasePool, conversation):
    """
    Create a new conversation or update history if conv_id exists.
    :param pool: DatabasePool
    :param conversation: (conv_id, user_id, title, history_json)
    :return: conversation id
    """
    sql = """ INSERT INTO conversations(conv_id, user_id, title, history)
              VALUES(?,?,?,?)
              ON CONFLICT(conv_id) DO UPDATE SET
              history=excluded.history,
              title=excluded.title """
    return await pool.execute_insert(sql, conversation)


async def get_user_conversation_count(pool: DatabasePool, user_id):
    """Query count of all conversations for a user."""
    result = await pool.execute_fetch_one(
        "SELECT COUNT(*) as count FROM conversations WHERE user_id=?;",
        (user_id,),
    )
    return result["count"] if result else 0


async def select_conversations_by_user(pool: DatabasePool, conversation_page):
    """
    Query conversations for a user by limit and offset.
    :param pool: DatabasePool
    :param conversation_page: (user_id, offset)
    """
    from config import ITEMS_PER_PAGE
    user_id, offset = conversation_page
    rows = await pool.execute_fetch_all(
        f"SELECT id, conv_id, user_id, title, history, created_at FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT {ITEMS_PER_PAGE} OFFSET ?;",
        (user_id, offset),
    )
    result = []
    for row in rows:
        # Count messages from history JSON
        msg_count = 0
        last_message = ""
        history_raw = row["history"]
        if history_raw:
            try:
                history = json.loads(history_raw)
                msg_count = len(history)
                # Get last user message as preview
                for entry in reversed(history):
                    if entry.get("role") == "user":
                        parts = entry.get("parts", [])
                        for p in parts:
                            if p.get("text"):
                                last_message = p["text"][:80]
                                break
                        if last_message:
                            break
            except (json.JSONDecodeError, TypeError):
                pass

        result.append({
            "id": row["id"],
            "conversation_id": row["conv_id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "message_count": msg_count,
            "last_message": last_message,
            "created_at": row["created_at"],
        })
    return result


async def select_conversation_by_id(pool: DatabasePool, conversation):
    """
    Query conversation by conv_id.
    :param pool: DatabasePool
    :param conversation: (user_id, conv_id)
    """
    row = await pool.execute_fetch_one(
        "SELECT conv_id, title, history FROM conversations WHERE user_id=? AND conv_id=?;",
        conversation,
    )
    if row:
        return {"conv_id": row["conv_id"], "title": row["title"], "history": row["history"]}
    return None


async def delete_conversation_by_id(pool: DatabasePool, conversation):
    """
    Delete conversation by conv_id.
    :param pool: DatabasePool
    :param conversation: (user_id, conv_id)
    """
    count = await pool.execute_delete(
        "DELETE FROM conversations WHERE user_id=? AND conv_id=?;", conversation
    )
    return count > 0


# --- Task Functions ---

async def create_task(pool: DatabasePool, task):
    """
    Create a new task.
    :param pool: DatabasePool
    :param task: (user_id, prompt, run_time, interval, plan_json, start_date)
    """
    sql = """ INSERT INTO tasks(user_id, prompt, run_time, interval, plan_json, start_date, status)
              VALUES(?,?,?,?,?,?,'active') """
    return await pool.execute_insert(sql, task)


async def get_all_tasks(pool: DatabasePool, batch_size: int = 100, offset: int = 0):
    """
    Retrieve active tasks from the database in batches.
    Skips completed 'once' tasks.
    """
    rows = await pool.execute_fetch_all(
        "SELECT id, user_id, prompt, run_time, interval, plan_json, start_date, status "
        "FROM tasks WHERE status='active' LIMIT ? OFFSET ?",
        (batch_size, offset),
    )
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "prompt": row["prompt"],
            "run_time": row["run_time"],
            "interval": row["interval"],
            "plan_json": row["plan_json"],
            "start_date": row["start_date"],
            "status": row["status"],
        }
        for row in rows
    ]


async def get_user_tasks(pool: DatabasePool, user_id):
    """Retrieve tasks for a specific user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, prompt, run_time, interval, plan_json, start_date FROM tasks WHERE user_id=? AND status='active'",
        (user_id,),
    )
    return [
        {
            "id": row["id"],
            "prompt": row["prompt"],
            "run_time": row["run_time"],
            "interval": row["interval"],
            "plan_json": row["plan_json"],
            "start_date": row["start_date"],
        }
        for row in rows
    ]


async def delete_task_by_id(pool: DatabasePool, task_specs):
    """
    Delete a task by its ID and user ID.
    :param pool: DatabasePool
    :param task_specs: (user_id, task_id)
    """
    count = await pool.execute_delete(
        "DELETE FROM tasks WHERE user_id=? AND id=?", task_specs
    )
    return count > 0


async def mark_task_completed(pool: DatabasePool, task_id: int):
    """Mark a task as completed."""
    await pool.execute(
        "UPDATE tasks SET status='completed' WHERE id=?", (task_id,)
    )


# --- User Functions ---

async def get_user(pool: DatabasePool, user_id):
    """Retrieve user settings, decrypting the API key."""
    row = await pool.execute_fetch_one(
        "SELECT user_id, api_key, model_name, grounding, system_instruction FROM users WHERE user_id=?",
        (user_id,),
    )
    if row:
        api_key = row["api_key"]
        if api_key:
            api_key = decrypt_api_key(api_key)
        return {
            "user_id": row["user_id"],
            "api_key": api_key,
            "model_name": row["model_name"],
            "grounding": row["grounding"],
            "system_instruction": row["system_instruction"],
        }
    return None


async def update_user_api_key(pool: DatabasePool, user_id, api_key):
    """Update or create user API key, encrypting before storage."""
    encrypted_key = encrypt_api_key(api_key)
    sql = """ INSERT INTO users(user_id, api_key)
              VALUES(?,?)
              ON CONFLICT(user_id) DO UPDATE SET
              api_key=excluded.api_key """
    await pool.execute(sql, (user_id, encrypted_key))


async def update_user_settings(pool: DatabasePool, user_id, model_name=None, grounding=None, system_instruction=None):
    """Update user settings."""
    updates = []
    params = []

    if model_name is not None:
        updates.append("model_name=?")
        params.append(model_name)
    if grounding is not None:
        updates.append("grounding=?")
        params.append(grounding)
    if system_instruction is not None:
        updates.append("system_instruction=?")
        params.append(system_instruction)

    if not updates:
        return

    params.append(user_id)
    sql = f"UPDATE users SET {', '.join(updates)} WHERE user_id=?"
    await pool.execute(sql, tuple(params))


# --- Knowledge Base Functions ---

async def add_knowledge(pool: DatabasePool, knowledge):
    """
    Add a new document to user knowledge base.
    :param pool: DatabasePool
    :param knowledge: (user_id, file_name, file_id, content_preview)
    """
    sql = "INSERT INTO knowledge_base(user_id, file_name, file_id, content_preview) VALUES(?,?,?,?)"
    return await pool.execute_insert(sql, knowledge)


async def get_user_knowledge(pool: DatabasePool, user_id):
    """Retrieve all knowledge documents for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, file_name, file_id, content_preview FROM knowledge_base WHERE user_id=?",
        (user_id,),
    )
    return [{"id": r["id"], "file_name": r["file_name"], "file_id": r["file_id"], "content_preview": r["content_preview"]} for r in rows]


async def delete_knowledge(pool: DatabasePool, user_id, doc_id):
    """Delete a knowledge document."""
    count = await pool.execute_delete(
        "DELETE FROM knowledge_base WHERE user_id=? AND id=?", (user_id, doc_id)
    )
    return count > 0


# --- Reminder Functions ---

async def add_reminder(pool: DatabasePool, reminder):
    """
    Add a new reminder.
    :param pool: DatabasePool
    :param reminder: (user_id, reminder_text, remind_at)
    """
    sql = "INSERT INTO reminders(user_id, reminder_text, remind_at) VALUES(?,?,?)"
    return await pool.execute_insert(sql, reminder)


async def get_pending_reminders(pool: DatabasePool):
    """Retrieve pending reminders that are due (optimized with time filter)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = await pool.execute_fetch_all(
        "SELECT id, user_id, reminder_text, remind_at FROM reminders WHERE status='pending' AND remind_at <= ?",
        (now,),
    )
    return [{"id": r["id"], "user_id": r["user_id"], "reminder_text": r["reminder_text"], "remind_at": r["remind_at"]} for r in rows]


async def update_reminder_status(pool: DatabasePool, reminder_id, status):
    """Update reminder status."""
    await pool.execute(
        "UPDATE reminders SET status=? WHERE id=?", (status, reminder_id)
    )


async def get_user_reminders(pool: DatabasePool, user_id):
    """Retrieve reminders for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, reminder_text, remind_at, status FROM reminders WHERE user_id=? ORDER BY remind_at DESC",
        (user_id,),
    )
    return [{"id": r["id"], "reminder_text": r["reminder_text"], "remind_at": r["remind_at"], "status": r["status"]} for r in rows]


async def delete_reminder(pool: DatabasePool, user_id, reminder_id):
    """Delete a reminder."""
    count = await pool.execute_delete(
        "DELETE FROM reminders WHERE user_id=? AND id=?", (user_id, reminder_id)
    )
    return count > 0
