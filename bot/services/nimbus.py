"""Keyed Nimbus Fetch fallback for media links."""

import asyncio
import logging
import os
from pathlib import Path

import httpx

from bot.services.downloader import VIDEO_LIMIT, local_bot_api
from bot.services.media_processor import finalize_media, label_media_path
from bot.services.models import MediaAsset
from bot.services.ready import asset_ready


logger = logging.getLogger(__name__)


async def nimbus_assets(link, directory: Path, stopped) -> list[MediaAsset]:
    base = os.getenv("NIMBUS_FETCH_API_URL", "").rstrip("/")
    key = os.getenv("NIMBUS_FETCH_API_KEY", "")
    if not base or not key:
        return []
    # Only the explicitly configured private service may receive the key.
    if base != "http://nimbus:8080":
        logger.warning("Nimbus URL rejected")
        return []
    headers = {"X-Nimbus-Key": key}
    try:
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as client:
            max_height = 2160 if local_bot_api() else 720
            response = await client.post("/api/v1/files/fetch", json={"url": link.url, "parent_id": "root", "mode": "video", "max_height": max_height})
            response.raise_for_status()
            job_id = response.json().get("job_id", "")
            if not isinstance(job_id, str) or not job_id:
                return []
            # Keep the fallback bounded; the next HTTPS backend should get a turn.
            for _ in range(20):
                if stopped.is_set():
                    raise InterruptedError("Stopped")
                await asyncio.sleep(2)
                response = await client.get(f"/api/v1/files/fetch/{job_id}")
                response.raise_for_status()
                job = response.json()
                if job.get("status") in {"done", "error"}:
                    break
            else:
                logger.info("platform=%s stage=nimbus error=TIMEOUT", link.platform)
                return []
            if job.get("status") != "done":
                return []
            nodes = job.get("nodes") or ([job["node"]] if job.get("node") else [])
            assets = []
            for index, node in enumerate(nodes[:20]):
                if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                    continue
                if node.get("size", 0) > VIDEO_LIMIT:
                    continue
                path = directory / f"nimbus-{index}.bin"
                async with client.stream("GET", f"/api/v1/files/{node['id']}/download", timeout=60) as stream:
                    stream.raise_for_status()
                    total = 0
                    with path.open("wb") as output:
                        async for chunk in stream.aiter_bytes(128 * 1024):
                            if stopped.is_set():
                                raise InterruptedError("Stopped")
                            total += len(chunk)
                            if total > VIDEO_LIMIT:
                                raise ValueError("Nimbus media too large")
                            output.write(chunk)
                mime_type = str(node.get("mime_type", ""))
                path = label_media_path(path, mime_type)
                kind = "photo" if mime_type.startswith("image/") else "video"
                kind, prepared, still = await asyncio.to_thread(finalize_media, path, kind, stopped)
                asset = MediaAsset(kind, prepared, "", link.platform, link.url, photo_path=still)
                await asset_ready(asset)
                assets.append(asset)
            return assets
    except InterruptedError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        logger.info("platform=%s stage=nimbus error=%s", link.platform, type(error).__name__)
        return []
