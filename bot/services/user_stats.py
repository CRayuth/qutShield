"""Persist the number of distinct Telegram users who have contacted the bot."""

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path

from aiogram import BaseMiddleware


logger = logging.getLogger(__name__)


class UserStats:
    def __init__(self, path: str | Path | None = None):
        database = Path(path or os.getenv("QUTSHIELD_STATS_DB", "data/user_stats.sqlite3"))
        database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        self._db.commit()

    def _record(self, user_id: int) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            self._db.commit()

    async def record(self, user_id: int) -> None:
        await asyncio.to_thread(self._record, user_id)

    def _count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    async def count(self) -> int:
        return await asyncio.to_thread(self._count)

    def close(self) -> None:
        with self._lock:
            self._db.close()


class UserStatsMiddleware(BaseMiddleware):
    def __init__(self, stats: UserStats):
        self.stats = stats

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            try:
                await self.stats.record(user.id)
            except sqlite3.Error:
                logger.exception("Could not record bot user")
        return await handler(event, data)
