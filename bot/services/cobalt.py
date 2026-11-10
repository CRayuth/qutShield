"""Optional, key-free fallback to the owner's private Cobalt service."""

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from bot.services.downloader import VIDEO_LIMIT, download_http, local_bot_api
from bot.services.media_processor import MediaProcessingError, finalize_media, label_media_path
from bot.services.models import MediaAsset
from bot.services.ready import asset_ready
from bot.services.url_detector import SourceLink


logger = logging.getLogger(__name__)
_PUBLIC_BASE = "https://dwnld.nichind.dev"
_PRIVATE_BASE = "http://cobalt:9000"


def _base() -> str:
    base = os.getenv("COBALT_API_URL", "").rstrip("/")
    return base if base in {_PUBLIC_BASE, _PRIVATE_BASE} else ""


def _allowed_tunnel(url: str) -> bool:
    base = _base()
    if not base:
        return False
    parts = urlsplit(url)
    expected = urlsplit(base)
    return (parts.scheme == expected.scheme and parts.hostname == expected.hostname
            and parts.port == expected.port and not parts.username and not parts.password)


async def _download_tunnel(client: httpx.AsyncClient, url: str, destination: Path, stopped, limit: int) -> str:
    current = url
    for _ in range(5):
        if stopped.is_set():
            raise InterruptedError("Stopped")
        if not _allowed_tunnel(current):
            if urlsplit(current).scheme == "https":
                return await download_http(current, destination, limit, stopped)
            raise ValueError("Cobalt returned an unsafe media URL")
        async with client.stream("GET", current, follow_redirects=False, timeout=40) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > limit:
                raise ValueError("Cobalt media is too large")
            total = 0
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes(128 * 1024):
                    if stopped.is_set():
                        raise InterruptedError("Stopped")
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Cobalt media is too large")
                    output.write(chunk)
            return response.headers.get("content-type", "").split(";")[0].lower()
    raise ValueError("Cobalt redirected too many times")


async def cobalt_assets(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    """Use Cobalt only for platforms with a verified private-service result."""
    base = _base()
    if not base or link.platform not in {"Facebook", "X"}:
        return []
    try:
        headers = {"Accept": "application/json"}
        if key := os.getenv("COBALT_API_KEY", "").strip():
            headers["Authorization"] = "Api-Key " + key
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), headers=headers) as client:
            response = await client.post(
                base + "/",
                json={"url": link.url, "videoQuality": "max" if local_bot_api() else "720", "downloadMode": "auto", "filenameStyle": "basic", "alwaysProxy": True},
            )
            if len(response.content) > 1024 * 1024:
                raise ValueError("Cobalt response is too large")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Cobalt returned invalid data")
            status = payload.get("status")
            if status == "error":
                details = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                logger.info("platform=%s stage=cobalt error=%s", link.platform, details.get("code", "UNKNOWN"))
                return []
            if status in {"tunnel", "redirect"}:
                entries = [(payload.get("url"), None)]
            elif status == "picker":
                entries = [(item.get("url"), item.get("type")) for item in payload.get("picker", [])[:20] if isinstance(item, dict)]
            else:
                logger.info("platform=%s stage=cobalt error=UNSUPPORTED_RESPONSE", link.platform)
                return []
            assets = []
            for index, (raw_url, declared_type) in enumerate(entries):
                if not isinstance(raw_url, str) or not raw_url:
                    continue
                used = sum(asset.path.stat().st_size for asset in assets)
                remaining = 160 * 1024 * 1024 - used
                if remaining <= 0:
                    break
                media_url = urljoin(base + "/", raw_url)
                path = directory / f"cobalt-{index}.bin"
                try:
                    content_type = await _download_tunnel(client, media_url, path, stopped, min(VIDEO_LIMIT, remaining))
                    if content_type.startswith("audio/"):
                        continue
                    path = label_media_path(path, content_type)
                    kind = "photo" if declared_type in {"photo", "image"} or content_type.startswith("image/") else "video"
                    kind, prepared, still = await asyncio.to_thread(finalize_media, path, kind, stopped)
                    asset = MediaAsset(kind, prepared, "", link.platform, link.url, photo_path=still)
                    await asset_ready(asset)
                    assets.append(asset)
                except (httpx.HTTPError, ValueError, MediaProcessingError) as error:
                    logger.info("platform=%s stage=cobalt_download error=%s", link.platform, type(error).__name__)
                finally:
                    if not any(asset.path == path for asset in assets):
                        path.unlink(missing_ok=True)
            return assets
    except InterruptedError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as error:
        logger.info("platform=%s stage=cobalt error=%s", link.platform, type(error).__name__)
        return []
