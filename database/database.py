import json
import re
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


@migration
async def m004_user_language_and_pinned_context(conn):
    """v4: Add language and pinned_context columns to users."""
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'auto'")
    except Exception:
        pass
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN pinned_context TEXT")
    except Exception:
        pass


@migration
async def m005_conversation_tags(conn):
    """v5: Create conversation_tags table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conv_id, user_id, tag)
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_tags_user ON conversation_tags(user_id);")


@migration
async def m006_user_shortcuts(conn):
    """v6: Create user_shortcuts table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_shortcuts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            command TEXT NOT NULL,
            response_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_shortcuts_user ON user_shortcuts(user_id);")


@migration
async def m007_recurring_reminders(conn):
    """v7: Add recurring_interval column to reminders."""
    try:
        await conn.execute("ALTER TABLE reminders ADD COLUMN recurring_interval TEXT")
    except Exception:
        pass


@migration
async def m008_bookmarks(conn):
    """v8: Create bookmarks table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conv_id TEXT,
            message_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id);")


@migration
async def m009_prompt_library(conn):
    """v9: Create prompt_library table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT DEFAULT 'general',
            title TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_prompts_user ON prompt_library(user_id);")


@migration
async def m010_feedback(conn):
    """v10: Create feedback table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message_preview TEXT,
            rating INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)


@migration
async def m011_url_monitors(conn):
    """v11: Create url_monitors table."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS url_monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            check_interval_hours INTEGER DEFAULT 1,
            last_hash TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_monitors_user ON url_monitors(user_id);")


@migration
async def m012_briefing_and_resume(conn):
    """v12: Add briefing_time to users and resume_index to conversations."""
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN briefing_time TEXT")
    except Exception:
        pass
    try:
        await conn.execute("ALTER TABLE conversations ADD COLUMN resume_index INTEGER")
    except Exception:
        pass


def _generate_hashtag(prompt: str, existing: set = None, task_id: int = None) -> str:
    """Generate a unique, meaningful hashtag from a task prompt.

    Takes first 2-3 significant words, CamelCases them, and appends
    a numeric suffix if needed to avoid collisions within `existing`.
    """
    # Strip non-alphanumeric, lowercase, split
    stop_words = {"a", "an", "the", "to", "for", "of", "in", "on", "and", "or", "my", "is", "how", "about", "with", "i", "me"}
    words = re.sub(r'[^a-zA-Z0-9\s]', '', prompt).lower().split()
    keywords = [w for w in words if w not in stop_words]
    if not keywords:
        keywords = words[:3]  # fallback to raw words
    # Take up to 3 keywords, capitalize each
    tag_base = "".join(w.capitalize() for w in keywords[:3])
    if not tag_base:
        tag_base = f"Task{task_id or 0}"

    existing = existing or set()
    tag = f"#{tag_base}"
    if tag.lower() not in {t.lower() for t in existing}:
        return tag
    # Add numeric suffix
    counter = 2
    while f"{tag}{counter}".lower() in {t.lower() for t in existing}:
        counter += 1
    return f"{tag}{counter}"


@migration
async def m013_task_hashtag(conn):
    """v13: Add hashtag column to tasks and populate existing rows."""
    try:
        await conn.execute("ALTER TABLE tasks ADD COLUMN hashtag TEXT")
    except Exception:
        pass  # Column already exists
    # Generate hashtags for existing tasks that don't have one
    cursor = await conn.execute("SELECT id, prompt, hashtag FROM tasks WHERE hashtag IS NULL")
    rows = await cursor.fetchall()
    existing_tags = set()
    for row in rows:
        tag = _generate_hashtag(row[1], existing_tags, task_id=row[0])
        existing_tags.add(tag)
        await conn.execute("UPDATE tasks SET hashtag=? WHERE id=?", (tag, row[0]))


@migration
async def m014_token_usage(conn):
    """v14: Create token_usage table for per-user token tracking."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            model_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_user ON token_usage(user_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(user_id, created_at);")


@migration
async def m015_context_cache(conn):
    """v15: Create context_cache table for per-user API cache tracking."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS context_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            cache_name TEXT NOT NULL UNIQUE,
            model_name TEXT,
            token_count INTEGER DEFAULT 0,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_context_cache_user ON context_cache(user_id);")


@migration
async def m016_knowledge_full_content(conn):
    """v16: Add full_content column to knowledge_base for caching and RAG."""
    try:
        await conn.execute("ALTER TABLE knowledge_base ADD COLUMN full_content TEXT")
    except Exception:
        pass  # Column already exists


@migration
async def m017_token_usage_cached(conn):
    """v17: Add cached_tokens column to token_usage."""
    try:
        await conn.execute("ALTER TABLE token_usage ADD COLUMN cached_tokens INTEGER DEFAULT 0")
    except Exception:
        pass


@migration
async def m018_thinking_mode(conn):
    """v18: Add thinking_mode column to users."""
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN thinking_mode TEXT DEFAULT 'off'")
    except Exception:
        pass


@migration
async def m019_token_usage_thinking(conn):
    """v19: Add thinking_tokens column to token_usage."""
    try:
        await conn.execute("ALTER TABLE token_usage ADD COLUMN thinking_tokens INTEGER DEFAULT 0")
    except Exception:
        pass


@migration
async def m020_code_execution(conn):
    """v20: Add code_execution column to users."""
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN code_execution INTEGER DEFAULT 0")
    except Exception:
        pass


@migration
async def m021_knowledge_chunks(conn):
    """v21: Create knowledge_chunks table for RAG embeddings."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            knowledge_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_user ON knowledge_chunks(user_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_knowledge ON knowledge_chunks(knowledge_id);")


# --- Multi-Provider Migrations ---

@migration
async def m022_active_provider(conn):
    """v22: Add active_provider column to users for multi-provider support."""
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN active_provider TEXT DEFAULT 'gemini'")
    except Exception:
        pass


@migration
async def m023_user_api_keys(conn):
    """v23: Create user_api_keys table for per-provider encrypted keys."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            base_url TEXT,
            is_valid INTEGER DEFAULT 1,
            last_validated_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, provider)
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_api_keys_user ON user_api_keys(user_id);")
    # Auto-migrate existing keys from users table
    await conn.execute("""
        INSERT OR IGNORE INTO user_api_keys (user_id, provider, api_key)
        SELECT user_id, 'gemini', api_key FROM users WHERE api_key IS NOT NULL AND api_key != ''
    """)


@migration
async def m024_user_provider_settings(conn):
    """v24: Create user_provider_settings table for per-provider model/settings."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_provider_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model_name TEXT,
            settings_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, provider)
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_provider_settings_user ON user_provider_settings(user_id);")


@migration
async def m025_token_usage_provider(conn):
    """v25: Add provider column to token_usage."""
    try:
        await conn.execute("ALTER TABLE token_usage ADD COLUMN provider TEXT")
    except Exception:
        pass


@migration
async def m026_conversations_provider(conn):
    """v26: Add provider and model_name columns to conversations."""
    try:
        await conn.execute("ALTER TABLE conversations ADD COLUMN provider TEXT")
    except Exception:
        pass
    try:
        await conn.execute("ALTER TABLE conversations ADD COLUMN model_name TEXT")
    except Exception:
        pass


@migration
async def m027_custom_providers(conn):
    """v27: Create custom_providers table for user-defined OpenAI-compatible endpoints."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            display_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        );
    """)


@migration
async def m028_estimated_cost(conn):
    """v28: Add estimated_cost_usd column to token_usage for monetization prep."""
    try:
        await conn.execute("ALTER TABLE token_usage ADD COLUMN estimated_cost_usd REAL")
    except Exception:
        pass


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
    :param task: (user_id, prompt, run_time, interval, plan_json, start_date, hashtag)
    """
    sql = """ INSERT INTO tasks(user_id, prompt, run_time, interval, plan_json, start_date, status, hashtag)
              VALUES(?,?,?,?,?,?,'active',?) """
    return await pool.execute_insert(sql, task)


async def get_all_tasks(pool: DatabasePool, batch_size: int = 100, offset: int = 0):
    """
    Retrieve active tasks from the database in batches.
    """
    rows = await pool.execute_fetch_all(
        "SELECT id, user_id, prompt, run_time, interval, plan_json, start_date, status, hashtag "
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
            "hashtag": row["hashtag"],
        }
        for row in rows
    ]


async def get_user_tasks(pool: DatabasePool, user_id):
    """Retrieve tasks for a specific user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, prompt, run_time, interval, plan_json, start_date, hashtag FROM tasks WHERE user_id=? AND status='active'",
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
            "hashtag": row["hashtag"],
        }
        for row in rows
    ]


async def get_user_task_hashtags(pool: DatabasePool, user_id) -> set:
    """Get all existing hashtags for a user's tasks."""
    rows = await pool.execute_fetch_all(
        "SELECT hashtag FROM tasks WHERE user_id=? AND hashtag IS NOT NULL",
        (user_id,),
    )
    return {row["hashtag"] for row in rows}


async def delete_task_by_hashtag(pool: DatabasePool, user_id: int, hashtag: str):
    """Delete a task by its hashtag and user ID. Returns the task_id if deleted."""
    row = await pool.execute_fetch_one(
        "SELECT id FROM tasks WHERE user_id=? AND hashtag=?", (user_id, hashtag)
    )
    if not row:
        return None
    task_id = row["id"]
    await pool.execute_delete(
        "DELETE FROM tasks WHERE user_id=? AND hashtag=?", (user_id, hashtag)
    )
    return task_id


async def mark_task_completed(pool: DatabasePool, task_id: int):
    """Mark a task as completed."""
    await pool.execute(
        "UPDATE tasks SET status='completed' WHERE id=?", (task_id,)
    )


async def get_task_by_id(pool: DatabasePool, task_id: int):
    """Retrieve a single task by its ID."""
    row = await pool.execute_fetch_one(
        "SELECT id, user_id, prompt, run_time, interval, plan_json, start_date, hashtag FROM tasks WHERE id=?",
        (task_id,),
    )
    if row:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "prompt": row["prompt"],
            "run_time": row["run_time"],
            "interval": row["interval"],
            "plan_json": row["plan_json"],
            "start_date": row["start_date"],
            "hashtag": row["hashtag"],
        }
    return None


# --- User Functions ---

async def get_user(pool: DatabasePool, user_id):
    """Retrieve user settings, decrypting the API key."""
    row = await pool.execute_fetch_one(
        "SELECT user_id, api_key, model_name, grounding, system_instruction, language, "
        "pinned_context, briefing_time, thinking_mode, code_execution "
        "FROM users WHERE user_id=?",
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
            "language": row.get("language", "auto"),
            "pinned_context": row.get("pinned_context"),
            "briefing_time": row.get("briefing_time"),
            "thinking_mode": row.get("thinking_mode", "off"),
            "code_execution": bool(row.get("code_execution", 0)),
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


async def update_user_settings(pool: DatabasePool, user_id, model_name=None, grounding=None, system_instruction=None, language=None, pinned_context=None, briefing_time=None, thinking_mode=None, code_execution=None):
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
    if language is not None:
        updates.append("language=?")
        params.append(language)
    if pinned_context is not None:
        updates.append("pinned_context=?")
        params.append(pinned_context)
    if thinking_mode is not None:
        updates.append("thinking_mode=?")
        params.append(thinking_mode)
    if code_execution is not None:
        updates.append("code_execution=?")
        params.append(int(code_execution))
    if briefing_time is not None:
        updates.append("briefing_time=?")
        params.append(briefing_time)

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
    :param reminder: (user_id, reminder_text, remind_at) or (user_id, reminder_text, remind_at, recurring_interval)
    """
    if len(reminder) == 4:
        sql = "INSERT INTO reminders(user_id, reminder_text, remind_at, recurring_interval) VALUES(?,?,?,?)"
    else:
        sql = "INSERT INTO reminders(user_id, reminder_text, remind_at) VALUES(?,?,?)"
    return await pool.execute_insert(sql, reminder)


async def get_pending_reminders(pool: DatabasePool):
    """Retrieve pending reminders that are due (optimized with time filter)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = await pool.execute_fetch_all(
        "SELECT id, user_id, reminder_text, remind_at, recurring_interval FROM reminders WHERE status='pending' AND remind_at <= ?",
        (now,),
    )
    return [{"id": r["id"], "user_id": r["user_id"], "reminder_text": r["reminder_text"], "remind_at": r["remind_at"], "recurring_interval": r.get("recurring_interval")} for r in rows]


async def update_reminder_status(pool: DatabasePool, reminder_id, status):
    """Update reminder status."""
    await pool.execute(
        "UPDATE reminders SET status=? WHERE id=?", (status, reminder_id)
    )


async def get_user_reminders(pool: DatabasePool, user_id):
    """Retrieve reminders for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, reminder_text, remind_at, status, recurring_interval FROM reminders WHERE user_id=? ORDER BY remind_at DESC",
        (user_id,),
    )
    return [{"id": r["id"], "reminder_text": r["reminder_text"], "remind_at": r["remind_at"], "status": r["status"], "recurring_interval": r.get("recurring_interval")} for r in rows]


async def delete_reminder(pool: DatabasePool, user_id, reminder_id):
    """Delete a reminder."""
    count = await pool.execute_delete(
        "DELETE FROM reminders WHERE user_id=? AND id=?", (user_id, reminder_id)
    )
    return count > 0


# --- Search Functions ---

async def search_conversations(pool: DatabasePool, user_id, query):
    """Search conversations by keyword in title or history."""
    rows = await pool.execute_fetch_all(
        "SELECT id, conv_id, title, created_at FROM conversations "
        "WHERE user_id=? AND (title LIKE ? OR history LIKE ?) ORDER BY id DESC LIMIT 20",
        (user_id, f"%{query}%", f"%{query}%"),
    )
    return [{"id": r["id"], "conversation_id": r["conv_id"], "title": r["title"], "created_at": r["created_at"]} for r in rows]


# --- Tag Functions ---

async def add_conversation_tag(pool: DatabasePool, user_id, conv_id, tag):
    """Tag a conversation."""
    sql = "INSERT OR IGNORE INTO conversation_tags(conv_id, user_id, tag) VALUES(?,?,?)"
    return await pool.execute_insert(sql, (conv_id, user_id, tag))


async def get_user_tags(pool: DatabasePool, user_id):
    """Get all distinct tags for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT DISTINCT tag FROM conversation_tags WHERE user_id=? ORDER BY tag",
        (user_id,),
    )
    return [r["tag"] for r in rows]


async def get_conversations_by_tag(pool: DatabasePool, user_id, tag):
    """Get conversations with a specific tag."""
    rows = await pool.execute_fetch_all(
        "SELECT c.id, c.conv_id, c.title, c.created_at FROM conversations c "
        "INNER JOIN conversation_tags ct ON c.conv_id = ct.conv_id AND c.user_id = ct.user_id "
        "WHERE c.user_id=? AND ct.tag=? ORDER BY c.id DESC",
        (user_id, tag),
    )
    return [{"id": r["id"], "conversation_id": r["conv_id"], "title": r["title"], "created_at": r["created_at"]} for r in rows]


async def get_conversation_tags(pool: DatabasePool, user_id, conv_id):
    """Get tags for a specific conversation."""
    rows = await pool.execute_fetch_all(
        "SELECT tag FROM conversation_tags WHERE user_id=? AND conv_id=?",
        (user_id, conv_id),
    )
    return [r["tag"] for r in rows]


async def remove_conversation_tag(pool: DatabasePool, user_id, conv_id, tag):
    """Remove a tag from a conversation."""
    count = await pool.execute_delete(
        "DELETE FROM conversation_tags WHERE conv_id=? AND user_id=? AND tag=?",
        (conv_id, user_id, tag),
    )
    return count > 0


# --- Shortcut Functions ---

async def add_shortcut(pool: DatabasePool, user_id, command, response_text):
    """Add a user shortcut."""
    sql = "INSERT INTO user_shortcuts(user_id, command, response_text) VALUES(?,?,?)"
    return await pool.execute_insert(sql, (user_id, command, response_text))


async def get_user_shortcuts(pool: DatabasePool, user_id):
    """Get all shortcuts for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, command, response_text FROM user_shortcuts WHERE user_id=?",
        (user_id,),
    )
    return [{"id": r["id"], "command": r["command"], "response_text": r["response_text"]} for r in rows]


async def delete_shortcut(pool: DatabasePool, user_id, shortcut_id):
    """Delete a shortcut."""
    count = await pool.execute_delete(
        "DELETE FROM user_shortcuts WHERE user_id=? AND id=?", (user_id, shortcut_id)
    )
    return count > 0


async def get_shortcut_by_command(pool: DatabasePool, user_id, command):
    """Get a shortcut by its command name."""
    row = await pool.execute_fetch_one(
        "SELECT id, command, response_text FROM user_shortcuts WHERE user_id=? AND command=?",
        (user_id, command),
    )
    if row:
        return {"id": row["id"], "command": row["command"], "response_text": row["response_text"]}
    return None


# --- Stats Functions ---

async def get_user_stats(pool: DatabasePool, user_id):
    """Get usage statistics for a user."""
    stats = {}
    r = await pool.execute_fetch_one("SELECT COUNT(*) as c FROM conversations WHERE user_id=?", (user_id,))
    stats["conversations"] = r["c"] if r else 0

    r = await pool.execute_fetch_one("SELECT COUNT(*) as c FROM tasks WHERE user_id=? AND status='active'", (user_id,))
    stats["active_tasks"] = r["c"] if r else 0

    r = await pool.execute_fetch_one("SELECT COUNT(*) as c FROM tasks WHERE user_id=?", (user_id,))
    stats["total_tasks"] = r["c"] if r else 0

    r = await pool.execute_fetch_one("SELECT COUNT(*) as c FROM reminders WHERE user_id=?", (user_id,))
    stats["total_reminders"] = r["c"] if r else 0

    r = await pool.execute_fetch_one("SELECT COUNT(*) as c FROM reminders WHERE user_id=? AND status='completed'", (user_id,))
    stats["completed_reminders"] = r["c"] if r else 0

    r = await pool.execute_fetch_one("SELECT COUNT(*) as c FROM knowledge_base WHERE user_id=?", (user_id,))
    stats["knowledge_docs"] = r["c"] if r else 0

    r = await pool.execute_fetch_one("SELECT MIN(created_at) as first FROM conversations WHERE user_id=?", (user_id,))
    stats["member_since"] = r["first"] if r and r["first"] else None

    return stats


# --- Bookmark Functions ---

async def add_bookmark(pool: DatabasePool, user_id, message_text, conv_id=None):
    """Add a bookmark."""
    sql = "INSERT INTO bookmarks(user_id, conv_id, message_text) VALUES(?,?,?)"
    return await pool.execute_insert(sql, (user_id, conv_id, message_text))


async def get_user_bookmarks(pool: DatabasePool, user_id):
    """Get all bookmarks for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, conv_id, message_text, created_at FROM bookmarks WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    )
    return [{"id": r["id"], "conv_id": r["conv_id"], "message_text": r["message_text"], "created_at": r["created_at"]} for r in rows]


async def delete_bookmark(pool: DatabasePool, user_id, bookmark_id):
    """Delete a bookmark."""
    count = await pool.execute_delete(
        "DELETE FROM bookmarks WHERE user_id=? AND id=?", (user_id, bookmark_id)
    )
    return count > 0


# --- Prompt Library Functions ---

async def add_prompt(pool: DatabasePool, user_id, title, prompt_text, category='general'):
    """Add a prompt to the library."""
    sql = "INSERT INTO prompt_library(user_id, category, title, prompt_text) VALUES(?,?,?,?)"
    return await pool.execute_insert(sql, (user_id, category, title, prompt_text))


async def get_user_prompts(pool: DatabasePool, user_id):
    """Get all saved prompts for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, category, title, prompt_text FROM prompt_library WHERE user_id=? ORDER BY category, title",
        (user_id,),
    )
    return [{"id": r["id"], "category": r["category"], "title": r["title"], "prompt_text": r["prompt_text"]} for r in rows]


async def delete_prompt(pool: DatabasePool, user_id, prompt_id):
    """Delete a prompt."""
    count = await pool.execute_delete(
        "DELETE FROM prompt_library WHERE user_id=? AND id=?", (user_id, prompt_id)
    )
    return count > 0


# --- Feedback Functions ---

async def add_feedback(pool: DatabasePool, user_id, message_preview, rating):
    """Add response feedback."""
    sql = "INSERT INTO feedback(user_id, message_preview, rating) VALUES(?,?,?)"
    return await pool.execute_insert(sql, (user_id, message_preview, rating))


# --- URL Monitor Functions ---

async def add_url_monitor(pool: DatabasePool, user_id, url, check_interval_hours=1):
    """Add a URL monitor."""
    sql = "INSERT INTO url_monitors(user_id, url, check_interval_hours) VALUES(?,?,?)"
    return await pool.execute_insert(sql, (user_id, url, check_interval_hours))


async def get_user_monitors(pool: DatabasePool, user_id):
    """Get all URL monitors for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, url, check_interval_hours, status, created_at FROM url_monitors WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    )
    return [{"id": r["id"], "url": r["url"], "check_interval_hours": r["check_interval_hours"], "status": r["status"], "created_at": r["created_at"]} for r in rows]


async def delete_url_monitor(pool: DatabasePool, user_id, monitor_id):
    """Delete a URL monitor."""
    count = await pool.execute_delete(
        "DELETE FROM url_monitors WHERE user_id=? AND id=?", (user_id, monitor_id)
    )
    return count > 0


async def get_active_monitors(pool: DatabasePool):
    """Get all active URL monitors."""
    rows = await pool.execute_fetch_all(
        "SELECT id, user_id, url, check_interval_hours, last_hash FROM url_monitors WHERE status='active'",
    )
    return [{"id": r["id"], "user_id": r["user_id"], "url": r["url"], "check_interval_hours": r["check_interval_hours"], "last_hash": r["last_hash"]} for r in rows]


async def update_monitor_hash(pool: DatabasePool, monitor_id, hash_value):
    """Update the last hash for a URL monitor."""
    await pool.execute("UPDATE url_monitors SET last_hash=? WHERE id=?", (hash_value, monitor_id))


# --- Conversation Resume Functions ---

async def update_conversation_resume(pool: DatabasePool, user_id, conv_id, resume_index):
    """Set resume point for a conversation."""
    await pool.execute(
        "UPDATE conversations SET resume_index=? WHERE user_id=? AND conv_id=?",
        (resume_index, user_id, conv_id),
    )


async def create_conversation_branch(pool: DatabasePool, user_id, source_conv_id, new_conv_id, title, history):
    """Create a branched copy of a conversation."""
    sql = "INSERT INTO conversations(conv_id, user_id, title, history) VALUES(?,?,?,?)"
    return await pool.execute_insert(sql, (new_conv_id, user_id, f"[Branch] {title}", history))


# --- Token Usage Functions ---

async def record_token_usage(pool: DatabasePool, user_id, prompt_tokens, completion_tokens, total_tokens, model_name=None, cached_tokens=0, thinking_tokens=0):
    """Record token usage for a user, including cached and thinking tokens."""
    sql = ("INSERT INTO token_usage(user_id, prompt_tokens, completion_tokens, total_tokens, "
           "model_name, cached_tokens, thinking_tokens) VALUES(?,?,?,?,?,?,?)")
    return await pool.execute_insert(sql, (user_id, prompt_tokens, completion_tokens, total_tokens, model_name, cached_tokens, thinking_tokens))


async def get_user_token_stats(pool: DatabasePool, user_id):
    """Get aggregated token usage statistics for a user, including cached and thinking tokens."""
    stats = {}

    r = await pool.execute_fetch_one(
        "SELECT COALESCE(SUM(prompt_tokens),0) as p, COALESCE(SUM(completion_tokens),0) as c, "
        "COALESCE(SUM(total_tokens),0) as t, COUNT(*) as n, "
        "COALESCE(SUM(cached_tokens),0) as cached, COALESCE(SUM(thinking_tokens),0) as thinking "
        "FROM token_usage WHERE user_id=?",
        (user_id,),
    )
    stats["prompt_tokens"] = r["p"] if r else 0
    stats["completion_tokens"] = r["c"] if r else 0
    stats["total_tokens"] = r["t"] if r else 0
    stats["total_requests"] = r["n"] if r else 0
    stats["cached_tokens"] = r["cached"] if r else 0
    stats["thinking_tokens"] = r["thinking"] if r else 0

    r = await pool.execute_fetch_one(
        "SELECT COALESCE(SUM(total_tokens),0) as t, COALESCE(SUM(cached_tokens),0) as cached "
        "FROM token_usage WHERE user_id=? AND DATE(created_at)=DATE('now')",
        (user_id,),
    )
    stats["today_tokens"] = r["t"] if r else 0
    stats["today_cached"] = r["cached"] if r else 0

    r = await pool.execute_fetch_one(
        "SELECT COALESCE(SUM(total_tokens),0) as t, COALESCE(SUM(cached_tokens),0) as cached "
        "FROM token_usage WHERE user_id=? AND created_at >= DATETIME('now', '-7 days')",
        (user_id,),
    )
    stats["week_tokens"] = r["t"] if r else 0
    stats["week_cached"] = r["cached"] if r else 0

    return stats


# --- Context Cache Functions ---

async def save_cache_record(pool: DatabasePool, user_id, cache_name, model_name, token_count, expires_at):
    """Save a context cache record."""
    sql = ("INSERT INTO context_cache(user_id, cache_name, model_name, token_count, expires_at) "
           "VALUES(?,?,?,?,?)")
    return await pool.execute_insert(sql, (user_id, cache_name, model_name, token_count, expires_at))


async def get_active_cache(pool: DatabasePool, user_id, model_name):
    """Get an active (non-expired) cache for a user and model."""
    row = await pool.execute_fetch_one(
        "SELECT id, cache_name, token_count, expires_at FROM context_cache "
        "WHERE user_id=? AND model_name=? AND expires_at > DATETIME('now') "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id, model_name),
    )
    if row:
        return {"cache_name": row["cache_name"], "token_count": row["token_count"], "expires_at": row["expires_at"]}
    return None


async def delete_cache_record(pool: DatabasePool, cache_name):
    """Delete a cache record by name."""
    await pool.execute_delete(
        "DELETE FROM context_cache WHERE cache_name=?", (cache_name,)
    )


# --- Knowledge Full Content Functions ---

async def add_knowledge_with_content(pool: DatabasePool, user_id, file_name, file_id, content_preview, full_content):
    """Add a knowledge document with full content for caching/RAG."""
    sql = ("INSERT INTO knowledge_base(user_id, file_name, file_id, content_preview, full_content) "
           "VALUES(?,?,?,?,?)")
    return await pool.execute_insert(sql, (user_id, file_name, file_id, content_preview, full_content))


async def get_user_knowledge_full(pool: DatabasePool, user_id):
    """Retrieve all knowledge documents with full content for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, file_name, file_id, content_preview, full_content FROM knowledge_base WHERE user_id=?",
        (user_id,),
    )
    return [{"id": r["id"], "file_name": r["file_name"], "file_id": r["file_id"],
             "content_preview": r["content_preview"], "full_content": r.get("full_content")} for r in rows]


# --- Knowledge Chunks / RAG Functions ---

async def save_knowledge_chunks(pool: DatabasePool, user_id, knowledge_id, chunks_with_embeddings):
    """Save chunked text with embeddings for a knowledge document.

    Args:
        chunks_with_embeddings: list of (chunk_index, chunk_text, embedding_json)
    """
    queries = []
    for chunk_index, chunk_text, embedding_json in chunks_with_embeddings:
        sql = ("INSERT INTO knowledge_chunks(user_id, knowledge_id, chunk_index, chunk_text, embedding) "
               "VALUES(?,?,?,?,?)")
        queries.append((sql, (user_id, knowledge_id, chunk_index, chunk_text, embedding_json)))
    if queries:
        await pool.execute_transaction(queries)


async def get_user_chunks_with_embeddings(pool: DatabasePool, user_id):
    """Get all knowledge chunks with embeddings for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, knowledge_id, chunk_index, chunk_text, embedding FROM knowledge_chunks WHERE user_id=?",
        (user_id,),
    )
    return [{"id": r["id"], "knowledge_id": r["knowledge_id"], "chunk_index": r["chunk_index"],
             "chunk_text": r["chunk_text"], "embedding": r["embedding"]} for r in rows]


async def delete_chunks_by_knowledge_id(pool: DatabasePool, knowledge_id):
    """Delete all chunks for a knowledge document."""
    await pool.execute_delete(
        "DELETE FROM knowledge_chunks WHERE knowledge_id=?", (knowledge_id,)
    )


# --- Multi-Provider Key Functions ---

async def get_user_api_key(pool: DatabasePool, user_id, provider):
    """Get API key for a specific provider."""
    row = await pool.execute_fetch_one(
        "SELECT api_key, base_url, is_valid FROM user_api_keys WHERE user_id=? AND provider=?",
        (user_id, provider),
    )
    if row:
        api_key = row["api_key"]
        if api_key:
            api_key = decrypt_api_key(api_key)
        return {"api_key": api_key, "base_url": row.get("base_url"), "is_valid": row.get("is_valid", 1)}
    return None


async def set_user_api_key(pool: DatabasePool, user_id, provider, api_key, base_url=None):
    """Set or update API key for a provider."""
    encrypted = encrypt_api_key(api_key)
    sql = """INSERT INTO user_api_keys (user_id, provider, api_key, base_url, last_validated_at)
             VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
             ON CONFLICT(user_id, provider) DO UPDATE SET
             api_key=excluded.api_key, base_url=excluded.base_url,
             is_valid=1, last_validated_at=CURRENT_TIMESTAMP"""
    await pool.execute(sql, (user_id, provider, encrypted, base_url))


async def get_user_providers(pool: DatabasePool, user_id):
    """Get all providers configured by a user."""
    rows = await pool.execute_fetch_all(
        "SELECT provider, is_valid, last_validated_at FROM user_api_keys WHERE user_id=?",
        (user_id,),
    )
    return [{"provider": r["provider"], "is_valid": r["is_valid"],
             "last_validated_at": r.get("last_validated_at")} for r in rows]


async def delete_user_api_key(pool: DatabasePool, user_id, provider):
    """Delete a user's API key for a provider."""
    await pool.execute_delete(
        "DELETE FROM user_api_keys WHERE user_id=? AND provider=?", (user_id, provider)
    )


async def get_user_provider_settings(pool: DatabasePool, user_id, provider):
    """Get provider-specific settings for a user."""
    row = await pool.execute_fetch_one(
        "SELECT model_name, settings_json FROM user_provider_settings WHERE user_id=? AND provider=?",
        (user_id, provider),
    )
    if row:
        settings = json.loads(row["settings_json"]) if row.get("settings_json") else {}
        return {"model_name": row.get("model_name"), "settings": settings}
    return None


async def set_user_provider_settings(pool: DatabasePool, user_id, provider, model_name=None, settings=None):
    """Set provider-specific settings for a user."""
    settings_json = json.dumps(settings) if settings else None
    sql = """INSERT INTO user_provider_settings (user_id, provider, model_name, settings_json)
             VALUES (?, ?, ?, ?)
             ON CONFLICT(user_id, provider) DO UPDATE SET
             model_name=COALESCE(excluded.model_name, model_name),
             settings_json=COALESCE(excluded.settings_json, settings_json)"""
    await pool.execute(sql, (user_id, provider, model_name, settings_json))


async def set_active_provider(pool: DatabasePool, user_id, provider):
    """Set the user's active provider."""
    await update_user_settings(pool, user_id)  # ensure user row exists
    await pool.execute(
        "UPDATE users SET active_provider=? WHERE user_id=?", (provider, user_id)
    )


# --- Custom Provider Functions ---

async def add_custom_provider(pool: DatabasePool, user_id, name, base_url, display_name=None):
    """Add a custom OpenAI-compatible provider."""
    sql = """INSERT INTO custom_providers (user_id, name, base_url, display_name)
             VALUES (?, ?, ?, ?)
             ON CONFLICT(user_id, name) DO UPDATE SET
             base_url=excluded.base_url, display_name=excluded.display_name"""
    return await pool.execute_insert(sql, (user_id, name, base_url, display_name))


async def get_user_custom_providers(pool: DatabasePool, user_id):
    """Get all custom providers for a user."""
    rows = await pool.execute_fetch_all(
        "SELECT id, name, base_url, display_name FROM custom_providers WHERE user_id=?",
        (user_id,),
    )
    return [{"id": r["id"], "name": r["name"], "base_url": r["base_url"],
             "display_name": r.get("display_name")} for r in rows]


async def delete_custom_provider(pool: DatabasePool, user_id, name):
    """Delete a custom provider."""
    await pool.execute_delete(
        "DELETE FROM custom_providers WHERE user_id=? AND name=?", (user_id, name)
    )


# --- Enhanced Token Usage ---

async def record_token_usage_with_provider(pool: DatabasePool, user_id, prompt_tokens, completion_tokens,
                                           total_tokens, model_name=None, provider=None,
                                           cached_tokens=0, thinking_tokens=0, estimated_cost_usd=None):
    """Record token usage with provider info and cost estimation."""
    sql = ("INSERT INTO token_usage(user_id, prompt_tokens, completion_tokens, total_tokens, "
           "model_name, provider, cached_tokens, thinking_tokens, estimated_cost_usd) "
           "VALUES(?,?,?,?,?,?,?,?,?)")
    return await pool.execute_insert(sql, (
        user_id, prompt_tokens, completion_tokens, total_tokens,
        model_name, provider, cached_tokens, thinking_tokens, estimated_cost_usd
    ))
