"""Small, safe helpers for temporary Telegram messages."""

import asyncio
import logging

from aiogram.exceptions import TelegramAPIError


logger = logging.getLogger(__name__)
_cleanup_tasks: set[asyncio.Task] = set()
_MAX_CLEANUPS = 100


async def delete_message(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError as error:
        logger.warning("Temporary message delete failed chat=%d msg=%d: %s", chat_id, message_id, type(error).__name__)


def delete_after(bot, chat_id: int, message_id: int, seconds: float = 5) -> None:
    async def cleanup() -> None:
        await asyncio.sleep(seconds)
        await delete_message(bot, chat_id, message_id)

    if len(_cleanup_tasks) >= _MAX_CLEANUPS:
        task = asyncio.create_task(delete_message(bot, chat_id, message_id))
    else:
        task = asyncio.create_task(cleanup())
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


async def temporary_status(bot, chat_id: int, message_id: int, text: str) -> None:
    """Edit the status message to *text*, then schedule deletion after 5 s.

    If editing fails (e.g. the message was already deleted by the user, or a
    transient Telegram API error), fall back to sending a fresh temporary
    message so the user always gets feedback instead of a stuck spinner.
    """
    edited = False
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
        edited = True
    except TelegramAPIError as error:
        logger.warning("Temporary message edit failed chat=%d msg=%d: %s — %s", chat_id, message_id, type(error).__name__, str(error)[:120])
    except Exception as error:
        logger.warning("Temporary message edit unexpected error chat=%d msg=%d: %s", chat_id, message_id, type(error).__name__)

    if edited:
        delete_after(bot, chat_id, message_id)
    else:
        # Fallback: send a standalone notice that auto-deletes after 5 s.
        try:
            reply = await bot.send_message(chat_id, text)
            delete_after(bot, chat_id, reply.message_id)
        except TelegramAPIError as error:
            logger.warning("Fallback notice also failed chat=%d: %s", chat_id, type(error).__name__)


async def failure(bot, chat_id: int, message_id: int, text: str = "❌ Unable to download this media.") -> None:
    await temporary_status(bot, chat_id, message_id, text)


async def complete(bot, chat_id: int, message_id: int, text: str = "🎉 Download complete.") -> None:
    await temporary_status(bot, chat_id, message_id, text)


async def notice(message, bot, text: str) -> None:
    try:
        reply = await message.answer(text)
    except TelegramAPIError as error:
        logger.warning("Temporary notice failed: %s", type(error).__name__)
        return
    delete_after(bot, reply.chat.id, reply.message_id)
