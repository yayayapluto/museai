"""Service container wired once at startup and stored on ``app.state.services``."""

from __future__ import annotations

from dataclasses import dataclass

from ..accounts.pool import AccountPool
from ..accounts.sqlite_store import SqliteAccountStore, SqliteKeyStore, SqliteUsageStore
from ..accounts.store import AccountStore, JsonAccountStore
from ..config import Settings
from ..core.media import MediaStore
from ..drivers.base import MuseDriver
from ..drivers.registry import create_driver
from .gateway import Gateway
from .tasks import TaskManager


@dataclass
class Services:
    settings: Settings
    driver: MuseDriver
    pool: AccountPool
    gateway: Gateway
    tasks: TaskManager
    media: MediaStore
    keys: SqliteKeyStore | None = None
    usage: SqliteUsageStore | None = None

    @classmethod
    def build(cls, settings: Settings, driver: MuseDriver | None = None) -> Services:
        settings.ensure_dirs()
        driver = driver or create_driver(settings)
        keys = usage = None
        if settings.account_store == "sqlite":
            keys = SqliteKeyStore(settings.db_file)
            usage = SqliteUsageStore(settings.db_file)
        store = _account_store(settings)
        pool = AccountPool(
            store,
            strategy=settings.pool_strategy,
            max_concurrency=settings.account_max_concurrency,
            cooldown=settings.account_cooldown,
            acquire_timeout=settings.pool_acquire_timeout,
        )
        return cls(
            settings=settings,
            driver=driver,
            pool=pool,
            gateway=Gateway(pool, driver, max_failover=settings.max_failover),
            tasks=TaskManager(settings.tasks_file),
            media=MediaStore(settings.media_dir),
            keys=keys,
            usage=usage,
        )


def _account_store(settings: Settings) -> AccountStore:
    if settings.account_store == "sqlite":
        return SqliteAccountStore(settings.db_file)
    return JsonAccountStore(settings.accounts_file)
