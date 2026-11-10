"""Run bounded media jobs while keeping the chat quiet."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

from bot.services.extractor import ExtractionError, extract_media
from bot.services.message_style import CHECKING, COMPLETE, QUEUED, UNSUPPORTED, UPLOADING, extraction_error, fetching
from bot.services.media_selection import create_previews, selection_keyboard, selection_text
from bot.services.ready import listener
from bot.services.status_messages import complete, delete_message, failure, notice
from bot.services.telegram_sender import send_assets
from bot.services.url_detector import SourceLink, candidate_urls, classify_url, resolve_link
from bot.state import Job, JobManager, QueuedJob, SelectionSession


router = Router()
logger = logging.getLogger(__name__)
SELECTION_TIMEOUT = 180


async def _set_job_status(bot: Bot, job: Job, text: str) -> None:
    if not job.status_message_id or job.stopped.is_set():
        return
    async with job.status_lock:
        if job.status_text == text:
            return
        try:
            await bot.edit_message_text(text=text, chat_id=job.chat_id, message_id=job.status_message_id)
            job.status_text = text
        except Exception as error:
            logger.debug("job_id=%s stage=status error=%s", job.id, type(error).__name__)


async def _keep_action(bot: Bot, chat_id: int, stopped, job: Job | None = None):
    """Resend chat-action every 4 s so the indicator stays visible."""
    while not stopped.is_set():
        if job is None or job.selection is None:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except TelegramAPIError:
                pass
        # Chat actions expire after 5 s; check stopped frequently so we
        # exit promptly when the job finishes.
        for _ in range(8):
            if stopped.is_set():
                return
            await asyncio.sleep(0.5)


async def _resolve_inputs(inputs, jobs: JobManager, job: Job) -> list[SourceLink]:
    if all(isinstance(item, SourceLink) for item in inputs):
        return list(dict.fromkeys(inputs))[:5]

    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5)) as client:
        async def resolve_one(value):
            async with jobs.resolve_slots:
                if job.stopped.is_set():
                    return None
                try:
                    return await resolve_link(value, client)
                except (ValueError, httpx.HTTPError):
                    return None

        found = await asyncio.gather(*(resolve_one(value) for value in inputs[:8]))
    links = []
    for link in found:
        if link and link.url not in {item.url for item in links}:
            links.append(link)
        if len(links) == 5:
            break
    return links


async def _choose_assets(bot: Bot, job: Job, assets: list, directory: Path, slots: asyncio.Semaphore) -> list:
    """Show a preview album, wait for the requester, then return only chosen files."""
    async with slots:
        previews = await create_previews(assets, directory, job.stopped)
    preview_ids = []
    selector_id = None
    try:
        for start in range(0, len(previews), 10):
            batch = previews[start:start + 10]
            if len(batch) == 1:
                sent = await bot.send_photo(job.chat_id, FSInputFile(batch[0]), caption=f"Preview {start + 1}")
                preview_ids.append(sent.message_id)
            else:
                try:
                    sent = await bot.send_media_group(
                        job.chat_id,
                        [InputMediaPhoto(media=FSInputFile(path), caption="Previews • tap numbers below to choose" if start == 0 and offset == 0 else None)
                         for offset, path in enumerate(batch)],
                    )
                    preview_ids.extend(item.message_id for item in sent)
                except TelegramAPIError:
                    for offset, path in enumerate(batch, start + 1):
                        sent = await bot.send_photo(job.chat_id, FSInputFile(path), caption=f"Preview {offset}")
                        preview_ids.append(sent.message_id)
        await _set_job_status(bot, job, "🖼 Previews ready. Choose items below.")
        prompt = await bot.send_message(
            job.chat_id, selection_text(set(), len(assets)),
            reply_markup=selection_keyboard(job.id, len(assets), set()),
        )
        selector_id = prompt.message_id
        session = SelectionSession(selector_id, len(assets), asyncio.get_running_loop().create_future())
        job.selection = session
        try:
            chosen = await asyncio.wait_for(session.result, timeout=SELECTION_TIMEOUT)
        except asyncio.TimeoutError:
            job.error_text = "⌛ Selection expired. Send the link again."
            return []
        except asyncio.CancelledError:
            if job.stopped.is_set():
                raise InterruptedError("Stopped") from None
            raise
        if not chosen:
            job.error_text = "⏭ No media selected."
        return [assets[index] for index in chosen]
    finally:
        job.selection = None
        await asyncio.gather(*(delete_message(bot, job.chat_id, message_id) for message_id in [*preview_ids, *([selector_id] if selector_id else [])]))


async def _process_one(index: int, link: SourceLink, bot: Bot, jobs: JobManager, job: Job, root: Path) -> bool:
    if job.stopped.is_set():
        return False
    directory = root / f"link-{index}"
    directory.mkdir()
    stage = "extraction"
    started = time.monotonic()
    collected = []
    seen = set()

    async def emit(asset):
        if job.stopped.is_set():
            raise InterruptedError("Stopped")
        if id(asset) not in seen:
            seen.add(id(asset))
            collected.append(replace(asset, source_url=link.display_url) if link.display_url else asset)

    token = listener.set(emit)
    try:
        try:
            async with jobs.slots:
                assets = await extract_media(link, directory, job.stopped)
                for asset in assets:
                    await emit(asset)
            total_bytes = sum(asset.path.stat().st_size for asset in collected)
            logger.info("job_id=%s platform=%s stage=extraction seconds=%.2f items=%d bytes=%d remote_candidates=%d", job.id, link.platform, time.monotonic() - started, len(collected), total_bytes, sum(bool(asset.remote_url) for asset in collected))
            if len(collected) > 1 and job.allow_selection:
                stage = "selection"
                async with job.selection_lock:
                    if job.stopped.is_set():
                        raise InterruptedError("Stopped")
                    collected = await _choose_assets(bot, job, collected, directory, jobs.slots)
            if not collected or job.stopped.is_set():
                return False
            stage = "telegram_upload"
            async with jobs.slots:
                await _set_job_status(bot, job, UPLOADING)
                delivered = await send_assets(bot, job.chat_id, collected, job.stopped)
            logger.info("job_id=%s platform=%s stage=telegram_upload delivered=%d expected=%d", job.id, link.platform, delivered, len(collected))
            return delivered > 0
        except InterruptedError:
            job.stopped.set()
            return False
        except (ExtractionError, ValueError, httpx.HTTPError, TelegramAPIError) as error:
            code = "TELEGRAM_UPLOAD_FAILED" if isinstance(error, TelegramAPIError) else getattr(error, "code", "EXTRACTION_FAILED" if stage == "extraction" else "DOWNLOAD_FAILED")
            job.error_text = extraction_error(code)
            source = urlsplit(link.url)
            source_ref = f"{source.hostname or ''}{source.path}"[:200]
            reason = re.sub(r"https?://\S+", "[link]", str(error))[:200]
            logger.warning("job_id=%s platform=%s source=%s stage=%s seconds=%.2f error=%s details=%s reason=%s", job.id, link.platform, source_ref, stage, time.monotonic() - started, code, type(error).__name__, reason)
            return False
        except Exception as error:
            logger.exception("job_id=%s platform=%s stage=%s error=UNEXPECTED details=%s", job.id, link.platform, stage, type(error).__name__)
            return False
    finally:
        listener.reset(token)
        shutil.rmtree(directory, ignore_errors=True)


async def process(message: Message, bot: Bot, inputs, jobs: JobManager, job: Job) -> None:
    action_task = asyncio.create_task(_keep_action(bot, job.chat_id, job.stopped, job))
    failed = False
    error_text = "❌ Couldn't download this media."
    started = time.monotonic()
    try:
        shared_dir = os.getenv("TELEGRAM_SHARED_FILES_DIR") or None
        with tempfile.TemporaryDirectory(prefix="qutshield-", dir=shared_dir) as temporary:
            root = Path(temporary)
            job.temporary_directory = root
            links = await _resolve_inputs(inputs, jobs, job)
            if not job.stopped.is_set():
                if not links:
                    failed = True
                    error_text = UNSUPPORTED
                else:
                    await _set_job_status(bot, job, fetching(links))
                    results = await asyncio.gather(*(_process_one(index, link, bot, jobs, job, root) for index, link in enumerate(links)))
                    failed = not all(results)
    except asyncio.CancelledError:
        job.stopped.set()
    except Exception:
        failed = True
        logger.exception("job_id=%s stage=job error=UNEXPECTED", job.id)
    finally:
        action_task.cancel()
        job.temporary_directory = None
        logger.info("job_id=%s stage=done seconds=%.2f", job.id, time.monotonic() - started)
        try:
            if not job.status_message_id:
                pass
            elif job.stopped.is_set():
                await delete_message(bot, job.chat_id, job.status_message_id)
            elif not failed:
                await complete(bot, job.chat_id, job.status_message_id, COMPLETE)
            else:
                await failure(bot, job.chat_id, job.status_message_id, job.error_text or error_text)
        except BaseException as error:
            logger.error("job_id=%s stage=cleanup error=%s", job.id, type(error).__name__)
        next_item = jobs.finish(job)
        if next_item:
            next_item.job.task = asyncio.create_task(process(next_item.message, bot, next_item.inputs, jobs, next_item.job))
            await _set_job_status(bot, next_item.job, CHECKING)


@router.callback_query(F.data.startswith("pick:"))
async def select_media(callback: CallbackQuery, bot: Bot, jobs: JobManager) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) not in {3, 4} or not isinstance(callback.message, Message):
        await callback.answer("Selection unavailable.", show_alert=True)
        return
    job = jobs.jobs.get((callback.message.chat.id, callback.from_user.id))
    session = job.selection if job and job.id == parts[1] else None
    if not session or session.message_id != callback.message.message_id or session.result.done():
        await callback.answer("Selection expired.", show_alert=True)
        return
    async with session.lock:
        if session.result.done():
            await callback.answer("Selection expired.", show_alert=True)
            return
        action = parts[2]
        if action == "toggle" and len(parts) == 4 and parts[3].isdigit():
            index = int(parts[3])
            if index >= session.count:
                await callback.answer("Invalid item.", show_alert=True)
                return
            if index in session.selected:
                session.selected.remove(index)
            else:
                session.selected.add(index)
            try:
                await bot.edit_message_text(
                    text=selection_text(session.selected, session.count),
                    chat_id=job.chat_id, message_id=session.message_id,
                    reply_markup=selection_keyboard(job.id, session.count, session.selected),
                )
            except TelegramAPIError:
                if index in session.selected:
                    session.selected.remove(index)
                else:
                    session.selected.add(index)
                await callback.answer("Couldn't update selection. Try again.", show_alert=True)
                return
            await callback.answer()
        elif action == "send" and len(parts) == 3:
            if not session.selected:
                await callback.answer("Select at least one item.", show_alert=True)
                return
            session.result.set_result(sorted(session.selected))
            await callback.answer("Sending selected media.")
        elif action == "all" and len(parts) == 3:
            session.result.set_result(list(range(session.count)))
            await callback.answer("Sending all media.")
        elif action == "skip" and len(parts) == 3:
            session.result.set_result([])
            await callback.answer("Skipped.")
        else:
            await callback.answer("Invalid selection.", show_alert=True)


@router.message()
async def handle_media(message: Message, bot: Bot, jobs: JobManager) -> None:
    hidden = [entity.url for entity in (*(message.entities or ()), *(message.caption_entities or ())) if entity.type == MessageEntityType.TEXT_LINK and entity.url]
    raw = candidate_urls(message.text or message.caption or "", hidden)
    if not raw:
        return
    if not any(classify_url(value) for value in raw):
        await notice(message, bot, UNSUPPORTED)
        return
    key = (message.chat.id, message.from_user.id if message.from_user else 0)
    active = key in jobs.jobs
    if not active and jobs.rate_limited(key):
        await notice(message, bot, "⏳ Wait a moment, then send another link.")
        return
    if not active and len(jobs.jobs) >= 20:
        await notice(message, bot, "🛠️ Server is busy. Try again soon.")
        return
    status_text = QUEUED if active else CHECKING
    status = await message.answer(status_text)
    job = Job(
        user_id=key[1], chat_id=key[0], status_message_id=status.message_id,
        allow_selection=message.chat.type == ChatType.PRIVATE and message.from_user is not None,
    )
    job.status_text = status_text
    result = jobs.submit(QueuedJob(job, message, raw))
    if result == "full":
        await delete_message(bot, status.chat.id, status.message_id)
        await notice(message, bot, "🕒 Queue full (5 waiting). Try again shortly.")
        return
    expected_text = CHECKING if result == "started" else QUEUED
    if result == "started":
        job.task = asyncio.create_task(process(message, bot, raw, jobs, job))
    if expected_text != status_text:
        await _set_job_status(bot, job, expected_text)
