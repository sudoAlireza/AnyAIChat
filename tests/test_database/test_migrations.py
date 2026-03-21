"""Tests for database migrations — verifies tables and columns after create_table."""
import pytest
import pytest_asyncio
import aiosqlite

from database.connection import DatabasePool
from database.database import create_table


class InMemoryPool(DatabasePool):
    """DatabasePool subclass using an in-memory SQLite database for testing."""

    def __init__(self):
        super().__init__(db_path=":memory:", max_connections=1)
        self._shared_conn = None

    async def get_connection(self) -> aiosqlite.Connection:
        if self._shared_conn is None:
            self._shared_conn = await aiosqlite.connect(":memory:")
            self._shared_conn.row_factory = aiosqlite.Row
        return self._shared_conn

    async def release_connection(self, conn: aiosqlite.Connection):
        # Don't close — keep the same in-memory connection across calls
        pass

    async def close_all(self):
        if self._shared_conn:
            await self._shared_conn.close()
            self._shared_conn = None


@pytest_asyncio.fixture
async def migrated_pool():
    """Create an in-memory pool and run all migrations."""
    pool = InMemoryPool()
    await create_table(pool)
    yield pool
    await pool.close_all()


async def _get_table_names(pool: InMemoryPool) -> set[str]:
    conn = await pool.get_connection()
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def _get_column_names(pool: InMemoryPool, table: str) -> set[str]:
    conn = await pool.get_connection()
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------

class TestTablesExist:
    @pytest.mark.asyncio
    async def test_users_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "users" in tables

    @pytest.mark.asyncio
    async def test_conversations_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "conversations" in tables

    @pytest.mark.asyncio
    async def test_token_usage_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "token_usage" in tables

    @pytest.mark.asyncio
    async def test_user_api_keys_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "user_api_keys" in tables

    @pytest.mark.asyncio
    async def test_user_provider_settings_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "user_provider_settings" in tables

    @pytest.mark.asyncio
    async def test_custom_providers_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "custom_providers" in tables

    @pytest.mark.asyncio
    async def test_user_tiers_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "user_tiers" in tables

    @pytest.mark.asyncio
    async def test_knowledge_base_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "knowledge_base" in tables

    @pytest.mark.asyncio
    async def test_reminders_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "reminders" in tables

    @pytest.mark.asyncio
    async def test_tasks_table_exists(self, migrated_pool):
        tables = await _get_table_names(migrated_pool)
        assert "tasks" in tables


# ---------------------------------------------------------------------------
# Column existence
# ---------------------------------------------------------------------------

class TestUsersColumns:
    @pytest.mark.asyncio
    async def test_active_provider_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "users")
        assert "active_provider" in columns

    @pytest.mark.asyncio
    async def test_user_id_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "users")
        assert "user_id" in columns

    @pytest.mark.asyncio
    async def test_api_key_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "users")
        assert "api_key" in columns

    @pytest.mark.asyncio
    async def test_model_name_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "users")
        assert "model_name" in columns

    @pytest.mark.asyncio
    async def test_thinking_mode_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "users")
        assert "thinking_mode" in columns

    @pytest.mark.asyncio
    async def test_language_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "users")
        assert "language" in columns


class TestTokenUsageColumns:
    @pytest.mark.asyncio
    async def test_provider_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "provider" in columns

    @pytest.mark.asyncio
    async def test_estimated_cost_usd_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "estimated_cost_usd" in columns

    @pytest.mark.asyncio
    async def test_prompt_tokens_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "prompt_tokens" in columns

    @pytest.mark.asyncio
    async def test_completion_tokens_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "completion_tokens" in columns

    @pytest.mark.asyncio
    async def test_cached_tokens_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "cached_tokens" in columns

    @pytest.mark.asyncio
    async def test_thinking_tokens_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "thinking_tokens" in columns

    @pytest.mark.asyncio
    async def test_model_name_column(self, migrated_pool):
        columns = await _get_column_names(migrated_pool, "token_usage")
        assert "model_name" in columns


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    @pytest.mark.asyncio
    async def test_schema_version_is_set(self, migrated_pool):
        conn = await migrated_pool.get_connection()
        cursor = await conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] > 0

    @pytest.mark.asyncio
    async def test_running_migrations_twice_is_idempotent(self, migrated_pool):
        # Running create_table again should not fail
        await create_table(migrated_pool)
        tables = await _get_table_names(migrated_pool)
        assert "users" in tables
