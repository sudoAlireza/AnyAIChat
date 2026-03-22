import asyncio
import logging
import aiosqlite
from config import DATABASE_PATH

logger = logging.getLogger(__name__)


class DatabasePool:
    """Async SQLite connection pool using semaphore-based concurrency control."""

    def __init__(self, db_path: str = None, max_connections: int = 5):
        self.db_path = db_path or DATABASE_PATH
        self._semaphore = asyncio.Semaphore(max_connections)
        self._connections: list[aiosqlite.Connection] = []
        self._lock = asyncio.Lock()

    async def get_connection(self) -> aiosqlite.Connection:
        """Get an async SQLite connection with WAL mode enabled."""
        await self._semaphore.acquire()
        try:
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA busy_timeout=5000")
            async with self._lock:
                self._connections.append(conn)
            return conn
        except Exception:
            self._semaphore.release()
            raise

    async def release_connection(self, conn: aiosqlite.Connection):
        """Release a connection back to the pool."""
        try:
            async with self._lock:
                if conn in self._connections:
                    self._connections.remove(conn)
            await conn.close()
        except Exception as e:
            logger.warning(f"Error releasing connection: {e}")
        finally:
            self._semaphore.release()

    async def close_all(self):
        """Close all connections in the pool."""
        async with self._lock:
            for conn in self._connections:
                try:
                    await conn.close()
                except Exception as e:
                    logger.warning(f"Error closing connection: {e}")
            self._connections.clear()
        logger.info("All database connections closed")

    async def execute(self, sql: str, params: tuple = None):
        """Execute a query and return the result."""
        conn = await self.get_connection()
        try:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            await conn.commit()
            return cursor
        finally:
            await self.release_connection(conn)

    async def execute_fetch_one(self, sql: str, params: tuple = None):
        """Execute a query and fetch one result."""
        conn = await self.get_connection()
        try:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            await self.release_connection(conn)

    async def execute_fetch_all(self, sql: str, params: tuple = None):
        """Execute a query and fetch all results."""
        conn = await self.get_connection()
        try:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            await self.release_connection(conn)

    async def execute_insert(self, sql: str, params: tuple = None) -> int:
        """Execute an insert and return lastrowid."""
        conn = await self.get_connection()
        try:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            await conn.commit()
            return cursor.lastrowid
        finally:
            await self.release_connection(conn)

    async def execute_delete(self, sql: str, params: tuple = None) -> int:
        """Execute a delete and return rowcount."""
        conn = await self.get_connection()
        try:
            if params:
                cursor = await conn.execute(sql, params)
            else:
                cursor = await conn.execute(sql)
            await conn.commit()
            return cursor.rowcount
        finally:
            await self.release_connection(conn)

    async def execute_transaction(self, queries: list):
        """Execute multiple queries in a single transaction."""
        conn = await self.get_connection()
        try:
            for sql, params in queries:
                if params:
                    await conn.execute(sql, params)
                else:
                    await conn.execute(sql)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self.release_connection(conn)
