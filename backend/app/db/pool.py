"""Database connection pool using asyncpg."""

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create the connection pool from DATABASE_URL.

    Reads DATABASE_URL and DATABASE_POOL_SIZE from environment.
    Returns the created pool and stores it as module-level singleton.
    """
    global _pool

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    # asyncpg expects a plain postgresql:// URI (no +asyncpg suffix)
    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")

    pool_size = int(os.environ.get("DATABASE_POOL_SIZE", "10"))

    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=pool_size,
    )
    return _pool


async def get_pool() -> asyncpg.Pool:
    """Get the existing pool.

    Raises RuntimeError if create_pool() has not been called.
    """
    if _pool is None:
        raise RuntimeError("Connection pool not initialized. Call create_pool() first.")
    return _pool


async def close_pool() -> None:
    """Close the pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
