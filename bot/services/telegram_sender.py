"""Upload media to Telegram with bounded retries."""

import asyncio
import logging
import os
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import FSInputFile, InputMediaLivePhoto, InputMediaPhoto, InputMediaVideo

logger = logging.getLogger(__name__)


def local_file_ref(path: Path) -> str | FSInputFile:
    """Return a zero-copy ``file://`` reference only if the server can read it.

    A self-hosted telegram-bot-api serves ``file://`` paths strictly under its
    ``--dir`` root (``TELEGRAM_WORK_DIR``), which is not the same location as
    the bot's shared temp mount. Passing a path outside that root is rejected
    with Bad Request, so the default is an ordinary multipart upload -- a local
    server still accepts large files that way, so nothing is lost.

    Set TELEGRAM_API_FILES_DIR to the server's own --dir (as seen from this
    container) to opt into zero-copy uploads once both sides share that root.
    """
    root = os.getenv("TELEGRAM_API_FILES_DIR", "").strip()
    if root:
        base = Path(root).resolve()
        resolved = path.resolve()
        if resolved.is_relative_to(base):
            return "file://" + str(resolved)
    return FSInputFile(path)


def caption_for(asset, collection: bool = False):
    lines = [asset.title.strip()[:450]] if asset.title.strip() else []
    icon = "🗂️" if collection else {"video": "🎬", "live_photo": "✨"}.get(asset.kind, "🖼️")
    return "\n".join([*lines, f"{icon} From {asset.platform}", asset.source_url])[:1024]


async def retry(action):
    for attempt in range(4):
        try:
            return await action()
        except TelegramRetryAfter as error:
            if attempt == 3:
                raise
            await asyncio.sleep(min(60, error.retry_after) + 1)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 3:
                raise
            await asyncio.sleep(2 ** attempt)


async def send_one(bot, chat_id, asset, use_remote=True, with_caption=True):
    caption = caption_for(asset) if with_caption else None

    async def deliver(media):
        if asset.kind == "video":
            await retry(lambda: bot.send_video(chat_id, media, caption=caption, supports_streaming=True, request_timeout=120))
        else:
            await retry(lambda: bot.send_photo(chat_id, media, caption=caption, request_timeout=120))

    if asset.kind == "live_photo" and asset.photo_path:
        try:
            result = await retry(lambda: bot.send_live_photo(
                chat_id,
                live_photo=local_file_ref(asset.path),
                photo=local_file_ref(asset.photo_path),
                caption=caption,
                request_timeout=120,
            ))
            logger.info("stage=telegram_live_photo bytes=%d confirmed=%s", asset.path.stat().st_size, bool(getattr(result, "live_photo", None)))
            return
        except TelegramBadRequest:
            logger.info("stage=live_photo_fallback")
            await retry(lambda: bot.send_video(chat_id, local_file_ref(asset.path), caption=caption, supports_streaming=True, request_timeout=120))
            return

    # Hosted Telegram recompresses URL fetches. The local Bot API should
    # upload the file we already validated so resolution stays intact.
    allow_remote = use_remote and asset.remote_url and not os.getenv("TELEGRAM_API_SERVER_URL", "").strip()
    if allow_remote:
        try:
            await deliver(asset.remote_url)
            logger.info("stage=telegram_fetch kind=%s bytes=%d", asset.kind, asset.path.stat().st_size)
            return
        except TelegramBadRequest:
            logger.info("stage=telegram_fetch_fallback kind=%s", asset.kind)
    await deliver(local_file_ref(asset.path))


async def send_assets(bot, chat_id, assets, stopped):
    sent = 0
    index = 0
    while index < len(assets) and not stopped.is_set():
        batch = []
        size = 0
        while index < len(assets) and len(batch) < 10:
            asset = assets[index]
            file_size = asset.path.stat().st_size
            if batch and size + file_size > 80 * 1024 * 1024:
                break
            batch.append(asset)
            size += file_size
            index += 1
        if len(batch) == 1:
            for asset in batch:
                if stopped.is_set():
                    break
                await send_one(bot, chat_id, asset)
                sent += 1
            continue
        def make_media(use_remote):
            media = []
            for position, asset in enumerate(batch):
                caption = caption_for(asset, collection=True) if position == 0 else None
                file = asset.remote_url if use_remote and asset.remote_url else local_file_ref(asset.path)
                if asset.kind == "live_photo" and asset.photo_path:
                    # aiogram types both fields as str, although its session
                    # can upload InputFile values in either field. Construct
                    # the model without field validation so both files become
                    # multipart attach:// references in sendMediaGroup.
                    media.append(InputMediaLivePhoto.model_construct(
                        media=FSInputFile(asset.path), photo=FSInputFile(asset.photo_path), caption=caption,
                    ))
                elif asset.kind == "video":
                    media.append(InputMediaVideo(media=file, caption=caption, supports_streaming=True))
                else:
                    media.append(InputMediaPhoto(media=file, caption=caption))
            return media

        media = make_media(not os.getenv("TELEGRAM_API_SERVER_URL", "").strip())
        has_live = any(asset.kind == "live_photo" for asset in batch)
        try:
            messages = await retry(lambda: bot.send_media_group(chat_id, media, request_timeout=120))
            if has_live:
                confirmed = sum(bool(getattr(message, "live_photo", None)) for asset, message in zip(batch, messages or ()) if asset.kind == "live_photo")
                logger.info("stage=telegram_live_photo_album confirmed=%d expected=%d", confirmed, sum(asset.kind == "live_photo" for asset in batch))
            sent += len(batch)
        except TelegramBadRequest:
            if any(asset.remote_url for asset in batch):
                logger.info("stage=telegram_fetch_fallback kind=album items=%d", len(batch))
                try:
                    messages = await retry(lambda: bot.send_media_group(chat_id, make_media(False), request_timeout=120))
                    if has_live:
                        confirmed = sum(bool(getattr(message, "live_photo", None)) for asset, message in zip(batch, messages or ()) if asset.kind == "live_photo")
                        logger.info("stage=telegram_live_photo_album confirmed=%d expected=%d", confirmed, sum(asset.kind == "live_photo" for asset in batch))
                    sent += len(batch)
                    continue
                except Exception:
                    if has_live:
                        raise
            if has_live:
                raise
            for asset in batch:
                if stopped.is_set():
                    break
                await send_one(bot, chat_id, asset, use_remote=False)
                sent += 1
        except Exception:
            if has_live:
                raise
            for asset in batch:
                if stopped.is_set():
                    break
                await send_one(bot, chat_id, asset)
                sent += 1
    return sent
