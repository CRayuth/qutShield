"""Read-only MTProto access to Telegram messages the Bot API cannot see.

A bot can only read chats it has been added to. This module uses the owner's
own Telegram session to fetch a message from a channel or group the bot is not
a member of, downloads its media, and hands it back so the *bot* still performs
the upload. Nothing here ever sends, edits, or deletes anything: the client is
used strictly for reads, so the owner's account never appears as the sender.
"""

import asyncio
import logging
import os
import re
from pathlib import Path

from bot.services.downloader import VIDEO_LIMIT
from bot.services.media_processor import finalize_media, label_media_path
from bot.services.models import MediaAsset
from bot.services.ready import asset_ready
from bot.services.url_detector import SourceLink

logger = logging.getLogger(__name__)

PUBLIC = re.compile(r"^https://t\.me/(?P<channel>[A-Za-z0-9_]{5,32})/(?P<id>\d+)$")
# t.me/c/<peer> omits the -100 prefix Telegram uses internally.
PRIVATE = re.compile(r"^https://t\.me/c/(?P<peer>\d+)/(?P<id>\d+)$")

_client = None
_lock = asyncio.Lock()


def configured() -> bool:
    return bool(_credentials())


def _session_reason() -> str:
    """Why the feature is off, for a precise operator-facing message."""
    path = os.getenv("TELEGRAM_MTPROTO_SESSION", "").strip()
    if not path:
        return "missing"
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return "missing"
    return _session_error(raw)


def _session_error(raw: str) -> str:
    """Explain why a session file cannot be used, or '' when it looks usable.

    Sessions exported by other MTProto libraries (gramjs, gotd/td, C# clients)
    are JSON with PascalCase fields and a base64 AuthKey. They are not
    interchangeable with Telethon's StringSession, and passing one through
    produced a confusing failure deep inside the connect handshake.
    """
    if not raw:
        return "empty"
    if raw.startswith("{"):
        return "foreign-format"
    try:
        from telethon.sessions import StringSession
    except ImportError:
        # Telethon is only needed to decode the payload; the JSON case above is
        # already caught without it, so unit tests can run without the package.
        return ""
    try:
        StringSession(raw)
    except Exception:
        return "undecodable"
    return ""


def _credentials():
    session_path = os.getenv("TELEGRAM_MTPROTO_SESSION", "").strip()
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not session_path or not api_id.isdigit() or not api_hash:
        return None
    path = Path(session_path)
    try:
        # The session string embeds a live authorization key.
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        session = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _session_error(session):
        return None
    return session, int(api_id), api_hash


async def _client_instance():
    """One shared client per process: reconnecting each job invites flood waits."""
    global _client
    async with _lock:
        if _client is not None and _client.is_user_authorized():
            return _client
        credentials = _credentials()
        if credentials is None:
            return None
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        session, api_id, api_hash = credentials
        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        if not client.is_user_authorized():
            await client.disconnect()
            logger.warning("stage=telegram_read error=AUTH_REQUIRED")
            return None
        await client.get_me()
        _client = client
        return client


async def _close():
    global _client
    async with _lock:
        if _client is not None:
            try:
                await _client.disconnect()
            except Exception:
                pass
            _client = None


def _target(url: str):
    public = PUBLIC.match(url)
    if public:
        return public.group("channel"), int(public.group("id"))
    private = PRIVATE.match(url)
    if private:
        # Peer ids in t.me/c/ links need the -100 channel prefix restored.
        return int(f"-100{private.group('peer')}"), int(private.group("id"))
    return None


def _live_video_thumb(message):
    """Telegram stores live photos as a still plus ``photo.video_sizes``."""
    sizes = getattr(getattr(message, "photo", None), "video_sizes", None) or ()
    return sizes[-1] if sizes else None


async def telegram_assets(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    from bot.services.extractor import ExtractionError

    target = _target(link.url)
    if target is None:
        raise ExtractionError("That Telegram link does not point to a post", "INVALID_URL")
    if not configured():
        reason = _session_reason()
        if reason == "foreign-format":
            raise ExtractionError(
                "That session file came from a different Telegram library. "
                "Generate a Telethon session for this bot.", "AUTH_REQUIRED")
        raise ExtractionError("Telegram reading is not set up on this bot yet", "AUTH_REQUIRED")

    client = await _client_instance()
    if client is None:
        raise ExtractionError("The Telegram session is no longer valid", "AUTH_REQUIRED")

    reference, message_id = target
    try:
        entity = await client.get_entity(reference)
        message = await client.get_messages(entity, ids=message_id)
    except (OverflowError, ValueError) as error:
        logger.info("stage=telegram_read error=INVALID_PEER")
        raise ExtractionError("I could not find that Telegram chat", "MEDIA_UNAVAILABLE") from error
    except Exception as error:
        # Telethon raises many transport-specific errors; name them, never log
        # the session or the api_hash.
        logger.info("stage=telegram_read error=%s", type(error).__name__)
        raise ExtractionError("I could not read that Telegram post", "MEDIA_UNAVAILABLE") from error

    if message is None or not getattr(message, "media", None):
        raise ExtractionError("That post has no media to send", "MEDIA_UNAVAILABLE")

    size = getattr(message.file, "size", 0) if getattr(message, "file", None) else 0
    if size and size > VIDEO_LIMIT:
        raise ExtractionError("That file is too large for Telegram", "FILE_TOO_LARGE")

    def progress(current, total):
        if stopped.is_set():
            raise InterruptedError("Stopped")
        if total and total > VIDEO_LIMIT:
            raise ExtractionError("That file is too large for Telegram", "FILE_TOO_LARGE")

    live_thumb = _live_video_thumb(message)
    destination = directory / ("telegram-live.mp4" if live_thumb is not None else "telegram-media.bin")
    try:
        download = {"file": str(destination), "progress_callback": progress}
        if live_thumb is not None:
            download["thumb"] = live_thumb
        saved = await message.download_media(**download)
    except InterruptedError:
        raise
    except Exception as error:
        logger.info("stage=telegram_download error=%s", type(error).__name__)
        raise ExtractionError("I could not download that Telegram media", "DOWNLOAD_FAILED") from error
    if not saved:
        raise ExtractionError("That post has no media to send", "MEDIA_UNAVAILABLE")

    path = Path(saved)
    mime_type = getattr(getattr(message, "file", None), "mime_type", "") or ""
    path = label_media_path(path, mime_type)
    kind = "video" if live_thumb is not None else ("photo" if bool(message.photo) or mime_type.startswith("image/") else "video")
    try:
        kind, prepared, still = await asyncio.to_thread(finalize_media, path, kind, stopped, live=live_thumb is not None)
    except Exception as error:
        logger.info("stage=telegram_prepare error=%s", type(error).__name__)
        raise ExtractionError("I could not prepare that Telegram media", "UNSUPPORTED_FORMAT") from error

    caption = (message.text or "")[:450] if isinstance(message.text, str) else ""
    asset = MediaAsset(kind, prepared, caption, "Telegram", link.url, photo_path=still)
    await asset_ready(asset)
    return [asset]
