"""Fast TikTok media via tikwm, the same class of API downloader bots use."""

import asyncio
import logging

import httpx

from bot.services.url_detector import SourceLink


logger = logging.getLogger(__name__)
_API = "https://www.tikwm.com/api/"


async def _tikwm_data(link: SourceLink) -> dict:
    """Fetch TikTok metadata, retrying a transient API failure once."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), headers={"User-Agent": "Mozilla/5.0"}) as client:
            for attempt in range(2):
                try:
                    response = await client.get(_API, params={"url": link.url, "hd": 1})
                    if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    break
                except (httpx.TimeoutException, httpx.ConnectError):
                    if attempt:
                        raise
                    await asyncio.sleep(0.5)
    except (httpx.HTTPError, ValueError, TypeError) as error:
        logger.info("platform=TikTok stage=tikwm error=%s", type(error).__name__)
        return {}
    if not isinstance(payload, dict) or payload.get("code") != 0:
        logger.info("platform=TikTok stage=tikwm error=NO_MEDIA")
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def tiktok_live_photo_pairs(link: SourceLink) -> list[tuple[str, str | None]]:
    """Return each still with its optional motion clip for a live photo post."""
    data = await _tikwm_data(link)
    images, clips = data.get("images"), data.get("live_images")
    if not isinstance(images, list) or not isinstance(clips, list):
        return []
    pairs = []
    for index, image in enumerate(images[:20]):
        if not isinstance(image, str) or not image.startswith("https://"):
            continue
        clip = clips[index] if index < len(clips) else None
        pairs.append((image, clip if isinstance(clip, str) and clip.startswith("https://") else None))
    return pairs if any(clip for _, clip in pairs) else []


async def tiktok_media_urls(link: SourceLink) -> list[tuple[str, str]]:
    """Return ordinary photo or video URLs."""
    data = await _tikwm_data(link)
    images = data.get("images")
    if isinstance(images, list):
        photos = [(item, "photo") for item in images if isinstance(item, str) and item.startswith("https://")]
        if photos:
            return photos[:20]
    # Try the standard rendition if the HD CDN URL has already expired.
    videos = list(dict.fromkeys(data[key] for key in ("hdplay", "play")
                                if isinstance(data.get(key), str) and data[key].startswith("https://")))
    return [(video, "video") for video in videos]
