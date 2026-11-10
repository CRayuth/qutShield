"""Friendly commands and Telegram-native start menu."""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.status_messages import delete_message, notice
from bot.services.user_stats import UserStats
from bot.state import JobManager


router = Router()
START_TEXT = "qutShield\n\nSend a link. I'll return its video or photos.\n\nYouTube • TikTok • Instagram\nFacebook • X"
HELP_TEXT = "❔ Send up to five links. In private chats, choose items from previews for multi-item posts. New messages queue while I work.\n\n/status shows your job and total users. /stop cancels your download and clears your queue."
START_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="❔ Help", callback_data="help"),
    InlineKeyboardButton(text="📡 Status", callback_data="status"),
]])
BACK_BUTTON = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⬅️ Back", callback_data="start"),
]])


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=START_BUTTONS)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(Command("status"))
async def status(message: Message, jobs: JobManager, user_stats: UserStats) -> None:
    await message.answer(await status_text(message.chat.id, message.from_user.id if message.from_user else 0, jobs, user_stats))


async def status_text(chat_id: int, user_id: int, jobs: JobManager, user_stats: UserStats) -> str:
    key = (chat_id, user_id)
    job = jobs.jobs.get(key)
    state = job.status_text if job and job.status_text else "🟢 Ready for links."
    waiting = len(jobs.pending.get(key, ()))
    lines = [state]
    if waiting:
        lines.append(f"🕒 Your waiting links: {waiting}")
    lines.append(f"👥 Total users since tracking began: {await user_stats.count():,}")
    return "\n".join(lines)


@router.callback_query(F.data.in_({"help", "status", "start"}))
async def menu_callback(callback: CallbackQuery, jobs: JobManager, user_stats: UserStats) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if callback.data == "help":
        await callback.message.edit_text(HELP_TEXT, reply_markup=BACK_BUTTON)
    elif callback.data == "status":
        await callback.message.edit_text(
            await status_text(callback.message.chat.id, callback.from_user.id, jobs, user_stats),
            reply_markup=BACK_BUTTON,
        )
    else:
        await callback.message.edit_text(START_TEXT, reply_markup=START_BUTTONS)


@router.message(Command("stop"))
async def stop(message: Message, bot: Bot, jobs: JobManager) -> None:
    key = (message.chat.id, message.from_user.id if message.from_user else 0)
    job = jobs.stop(key)
    if job is None:
        await notice(message, bot, "🟢 No download is running.")
        return
    await delete_message(bot, job.chat_id, job.status_message_id)
    for status_id in job.cancelled_status_ids:
        await delete_message(bot, job.chat_id, status_id)
    await notice(message, bot, "🛑 Download cancelled. Queue cleared.")
