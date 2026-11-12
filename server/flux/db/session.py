"""数据库会话与引擎（主规格 §11.1 存储选型 / §17.3 优先异步）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from flux.logging import get_logger

logger = get_logger(__name__)


def create_engine(database_url: str, *, connect_timeout: float | None = None) -> AsyncEngine:
    """创建异步引擎。

    SQLite（本地开发）不支持连接池参数，故按方言分支。
    `connect_timeout` 用于探针等需要快速失败的场景：PostgreSQL 下传给 asyncpg
    作为单次连接超时，避免不可达主机长时间挂起。
    """
    if database_url.startswith("sqlite"):
        # timeout 是 sqlite3.connect 的 busy 超时（秒）：默认 5 秒，Windows 上磁盘与
        # 线程调度更慢，并发写（心跳 / 令牌撤销 / 终态落库）容易直接被打成 locked。
        engine = create_async_engine(database_url, future=True, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _config_sqlite(dbapi_connection, _record):  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            # SQLite 默认不强制外键，本地会静默吞掉违反外键的写入；开启后与 PostgreSQL 行为一致。
            cursor.execute("PRAGMA foreign_keys=ON")
            # 默认 rollback journal 下读事务会阻塞写提交：测试里 20ms 一次的轮询读会把
            # 后台心跳 UPDATE 饿到 busy 超时（Windows CI 实测 database is locked）。
            # WAL 让读写互不阻塞，写写仍串行，由上面的 busy 超时兜住。
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    connect_args = {"timeout": connect_timeout} if connect_timeout is not None else {}
    return create_async_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        connect_args=connect_args,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database(engine: AsyncEngine) -> bool:
    """就绪探针：能否真正连上库。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - 探针需要把任何连接异常都转为不可用
        logger.warning("数据库就绪探针失败", exc_info=True)
        return False


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
