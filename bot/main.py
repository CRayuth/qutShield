"""Bot entry point."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from dotenv import load_dotenv

from bot.handlers import commands, media
from bot.services.user_stats import UserStats, UserStatsMiddleware
from bot.state import JobManager


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for name in ("httpx", "httpcore", "aiohttp", "aiogram.client"):
    logging.getLogger(name).setLevel(logging.WARNING)


def api_session() -> AiohttpSession | None:
    """Build a session for a self-hosted Bot API server, or None for hosted.

    A local telegram-bot-api instance raises the bot upload ceiling from the
    hosted 50 MB to roughly 2 GB, which is what lets the extractors keep the
    original rendition instead of pre-emptively picking a lower one.
    """
    base = os.getenv("TELEGRAM_API_SERVER_URL", "").strip().rstrip("/")
    if not base:
        return None
    scheme, _, rest = base.partition("://")
    if scheme not in {"http", "https"} or not rest or "@" in rest or "/" in rest:
        raise RuntimeError("TELEGRAM_API_SERVER_URL must be a bare http(s) host:port")
    return AiohttpSession(api=TelegramAPIServer.from_base(base))


async def run() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "replace-with-your-botfather-token":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
    session = api_session()
    if session is None:
        bot = Bot(token=token)
    else:
        # Media uploads use shared file:// paths in telegram_sender.
        bot = Bot(token=token, session=session)
        logging.getLogger(__name__).info("using self-hosted Bot API at %s", os.getenv("TELEGRAM_API_SERVER_URL", "").strip())
    dispatcher = Dispatcher()
    stats = UserStats()
    dispatcher.message.outer_middleware(UserStatsMiddleware(stats))
    dispatcher.callback_query.outer_middleware(UserStatsMiddleware(stats))
    dispatcher.include_router(commands.router)
    dispatcher.include_router(media.router)
    jobs = JobManager()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, jobs=jobs, user_stats=stats)
    finally:
        stats.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())
