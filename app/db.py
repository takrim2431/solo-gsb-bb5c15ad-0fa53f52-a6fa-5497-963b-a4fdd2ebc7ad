"""数据库连接池（asyncpg）。"""

import asyncio

import asyncpg

from .config import settings

_pool: "asyncpg.Pool | None" = None


async def init_pool() -> asyncpg.Pool:
    """创建连接池；数据库未就绪时按配置重试，便于容器编排下随库一起启动。"""
    global _pool
    last_exc: Exception | None = None
    for _ in range(settings.db_connect_retries):
        try:
            _pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                min_size=settings.db_pool_min_size,
                max_size=settings.db_pool_max_size,
            )
            return _pool
        except (OSError, asyncpg.PostgresError) as exc:  # 连接失败/库未就绪
            last_exc = exc
            await asyncio.sleep(1)
    raise RuntimeError(f"cannot connect to database after retries: {last_exc}")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """FastAPI 依赖注入用。"""
    assert _pool is not None, "database pool is not initialized"
    return _pool
