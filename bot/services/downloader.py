"""Bounded HTTP downloads with redirect and address checks."""

import asyncio
import os
from pathlib import Path
from urllib.parse import urljoin

import httpx

from bot.services.url_detector import validate_public_url


def local_bot_api() -> bool:
    """True when a self-hosted telegram-bot-api server is configured."""
    return bool(os.getenv("TELEGRAM_API_SERVER_URL", "").strip())


PHOTO_LIMIT = 10 * 1024 * 1024
# The hosted Bot API refuses bot uploads above 50 MB. A self-hosted server
# running in local mode accepts roughly 2 GB, so the extractors can stop
# pre-emptively selecting a lower rendition just to fit the hosted cap.
VIDEO_LIMIT = 2000 * 1024 * 1024 if local_bot_api() else 50 * 1024 * 1024


async def download_http(url: str, destination: Path, limit: int, stopped, headers: dict | None = None, client: httpx.AsyncClient | None = None) -> str:
    if client is not None:
        return await _download_with_client(url, destination, limit, stopped, headers, client)
    async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False) as own_client:
        return await _download_with_client(url, destination, limit, stopped, headers, own_client)


async def _download_with_client(url: str, destination: Path, limit: int, stopped, headers: dict | None, client: httpx.AsyncClient) -> str:
    current = url
    request_headers = {key: value for key, value in (headers or {}).items() if key.lower() not in {"host", "authorization", "cookie"}}
    for attempt in range(3):
        for _ in range(5):
            if stopped.is_set():
                raise InterruptedError("Stopped")
            await asyncio.to_thread(validate_public_url, current)
            try:
                async with client.stream("GET", current, headers=request_headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        current = urljoin(current, response.headers.get("location", ""))
                        continue
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        break
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length and int(length) > limit:
                        raise ValueError("That file is too large for Telegram")
                    total = 0
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes(128 * 1024):
                            if stopped.is_set():
                                raise InterruptedError("Stopped")
                            total += len(chunk)
                            if total > limit:
                                raise ValueError("That file is too large for Telegram")
                            output.write(chunk)
                    return response.headers.get("content-type", "").split(";")[0].lower()
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt == 2:
                    raise
                break
        else:
            raise ValueError("That media link redirected too many times")
        await asyncio.sleep(2 ** attempt)
    raise ValueError("I couldn't download that file right now")
