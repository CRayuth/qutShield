"""Run bounded media jobs while keeping the chat quiet."""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from aiogram import Bot, Router
from aiogram.enums import ChatAction, MessageEntityType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from bot.services.extractor import ExtractionError, extract_media
from bot.services.message_style import CHECKING, COMPLETE, QUEUED, UNSUPPORTED, UPLOADING, extraction_error, fetching
from bot.services.ready import listener
from bot.services.status_messages import complete, delete_message, failure, notice
from bot.services.telegram_sender import send_assets
from bot.services.url_detector import SourceLink, candidate_urls, classify_url, resolve_link
from bot.state import Job, JobManager, QueuedJob


router = Router()
logger = logging.getLogger(__name__)


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


async def _keep_action(bot: Bot, chat_id: int, stopped):
    """Resend chat-action every 4 s so the indicator stays visible."""
    while not stopped.is_set():
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


async def _process_one(index: int, link: SourceLink, bot: Bot, jobs: JobManager, job: Job, root: Path) -> bool:
    async with jobs.slots:
        if job.stopped.is_set():
            return False
        directory = root / f"link-{index}"
        directory.mkdir()
        stage = "extraction"
        started = time.monotonic()
        delivered = 0
        sent = set()
        buffer = []
        send_lock = asyncio.Lock()

        async def flush_buffer():
            nonlocal delivered, buffer
            if not buffer:
                return
            batch, buffer = buffer, []
            await _set_job_status(bot, job, UPLOADING)
            delivered += await send_assets(bot, job.chat_id, batch, job.stopped)

        async def emit(asset):
            nonlocal stage
            if id(asset) in sent:
                return
            if job.stopped.is_set():
                raise InterruptedError("Stopped")
            stage = "telegram_upload"
            async with send_lock:
                if id(asset) in sent:
                    return
                sent.add(id(asset))
                buffer.append(replace(asset, source_url=link.display_url) if link.display_url else asset)
                if len(buffer) >= 10:
                    await flush_buffer()

        token = listener.set(emit)
        try:
            try:
                assets = await extract_media(link, directory, job.stopped)
                total_bytes = sum(asset.path.stat().st_size for asset in assets)
                logger.info("job_id=%s platform=%s stage=extraction seconds=%.2f items=%d bytes=%d remote_candidates=%d", job.id, link.platform, time.monotonic() - started, len(assets), total_bytes, sum(bool(asset.remote_url) for asset in assets))
                for asset in assets:
                    await emit(asset)
                async with send_lock:
                    await flush_buffer()
                logger.info("job_id=%s platform=%s stage=telegram_upload delivered=%d expected=%d", job.id, link.platform, delivered, len(assets))
                return delivered > 0
            except InterruptedError:
                job.stopped.set()
                return False
            except (ExtractionError, ValueError, httpx.HTTPError, TelegramAPIError) as error:
                code = "TELEGRAM_UPLOAD_FAILED" if isinstance(error, TelegramAPIError) else getattr(error, "code", "EXTRACTION_FAILED" if stage == "extraction" else "DOWNLOAD_FAILED")
                job.error_text = extraction_error(code)
                reason = getattr(error, "message", None) or str(error)
                source = urlsplit(link.url)
                # Keep the media path/ID for troubleshooting, but omit query
                # parameters that may contain share tokens or tracking data.
                source_ref = f"{source.hostname or ''}{source.path}"[:200]
                logger.warning("job_id=%s platform=%s source=%s stage=%s seconds=%.2f error=%s details=%s reason=%.200s", job.id, link.platform, source_ref, stage, time.monotonic() - started, code, type(error).__name__, reason)
                return False
            except Exception as error:
                logger.exception("job_id=%s platform=%s stage=%s error=UNEXPECTED details=%s", job.id, link.platform, stage, type(error).__name__)
                return False
        finally:
            listener.reset(token)
            shutil.rmtree(directory, ignore_errors=True)


async def process(message: Message, bot: Bot, inputs, jobs: JobManager, job: Job) -> None:
    action_task = asyncio.create_task(_keep_action(bot, job.chat_id, job.stopped))
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
    job = Job(user_id=key[1], chat_id=key[0], status_message_id=status.message_id)
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
