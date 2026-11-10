"""Extract public media using yt-dlp, with gallery and Instagram fallbacks."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

from bot.instagram import BROWSER_UA, InstagramSource, parse_instagram_url
from bot.services.cobalt import cobalt_assets
from bot.services.nimbus import nimbus_assets
from bot.services.tiktok import tiktok_live_photo_pairs, tiktok_media_urls
from bot.services.telegram_reader import telegram_assets
from bot.services.cookies import cookie_file, http_cookies
from bot.services.downloader import PHOTO_LIMIT, VIDEO_LIMIT, download_http, local_bot_api
from bot.services.media_processor import MediaProcessingError, finalize_media, label_media_path
from bot.services.models import MediaAsset
from bot.services.network import is_proxy_connection_error
from bot.services.ready import asset_ready
from bot.services.url_detector import SourceLink, validate_public_url


logger = logging.getLogger(__name__)


class ExtractionError(ValueError):
    def __init__(self, message: str, code: str = "EXTRACTION_FAILED"):
        super().__init__(message)
        self.code = code


def _safe_detail(error: Exception | str) -> str:
    return re.sub(r"https?://\S+", "[link]", str(error))[:240]


class _YtLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        logger.debug("yt-dlp warning: %s", _safe_detail(message))

    def error(self, message):
        logger.debug("yt-dlp error: %s", _safe_detail(message))


async def _facebook_story_requires_login(link: SourceLink) -> bool:
    """Check the obvious login redirect before trying slower extractors."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4, connect=3), follow_redirects=False, cookies=http_cookies("Facebook")) as client:
            async with client.stream("GET", link.url) as response:
                if response.status_code >= 500:
                    raise ExtractionError("Facebook is unavailable right now", "DOWNLOAD_FAILED")
                return "login.php" in response.headers.get("location", "").lower()
    except httpx.HTTPError as error:
        logger.info("platform=Facebook stage=story_check error=%s", type(error).__name__)
        if is_proxy_connection_error(error, link.url):
            raise ExtractionError("The configured network proxy is unreachable", "NETWORK_PROXY_UNAVAILABLE") from error
        raise ExtractionError("I couldn't reach Facebook right now", "DOWNLOAD_FAILED") from error


def _format_order() -> str:
    """Pick the highest useful rendition the Bot API can actually deliver.

    On the self-hosted server, file size is not the constraint (~2 GB). TikTok
    and YouTube often put 1080p+ in HEVC/VP9 and only 720p in H.264; leading
    with avc1 therefore silently downgraded those links. Prefer 1080p+ first,
    then same-height H.264, then any remaining stream.

    The hosted Bot API still has a 50 MB ceiling, so that path keeps a 720p
    H.264 cap to stay uploadable without a re-encode.
    """
    if local_bot_api():
        return (
            "bv*[height>=1080][ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
            "/bv*[height>=1080][ext=mp4]+ba[ext=m4a]"
            "/bv*[height>=1080]+ba"
            "/bv[ext=mp4][vcodec^=avc1]+ba[ext=m4a]"
            "/b[ext=mp4][vcodec^=avc1]"
            "/bv+ba/b"
        )
    return (
        "bv[height<=720][ext=mp4][vcodec^=avc1][filesize<35M]+ba[ext=m4a][filesize<8M]"
        "/b[height<=720][ext=mp4][filesize<45M]"
        "/bv[height<=480][ext=mp4][vcodec^=avc1][filesize<40M]+ba[ext=m4a]"
        "/b[height<=480][ext=mp4][filesize<45M]"
        "/bv[height<=480]+ba/b"
    )


def _tiktok_format() -> str:
    """TikTok already muxes video+audio. Forcing bv+ba makes yt-dlp merge twice."""
    if local_bot_api():
        return "b[height>=1080][ext=mp4]/b[ext=mp4]/b"
    return "b[height<=720][ext=mp4][filesize<45M]/b[ext=mp4]/b"


def _job_cookie_file(platform: str, directory: Path) -> Path | None:
    """A private writable cookie jar, re-seeded from the original every job.

    yt-dlp writes the jar back after each request. A durable copy let a single
    bad server-side rotation persist permanently: when Instagram cleared the
    session, the stored copy lost its sessionid and every later job ran logged
    out. Copying per job discards that damage, and the read-only original stays
    authoritative until the owner replaces it.
    """
    source = cookie_file(platform)
    if source is None:
        return None
    target = directory / f"cookies-{platform}.txt"
    try:
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    except OSError:
        return None
    return target


def _impersonate_targets(platform: str):
    """TLS fingerprints yt-dlp should try, in order.

    Facebook started rejecting generic Chrome impersonation in late 2026
    (``Cannot parse data``); chrome-110 / chrome-99 / firefox still parse.
    Instagram wants a Firefox TLS/UA match (Burp-style Mozilla). TikTok
    keeps generic Chrome.
    """
    if platform == "Instagram":
        return [ImpersonateTarget.from_str("firefox"), None]
    if platform == "Facebook":
        return [
            ImpersonateTarget.from_str("chrome-110"),
            ImpersonateTarget.from_str("chrome-99"),
            ImpersonateTarget.from_str("firefox"),
        ]
    return [ImpersonateTarget.from_str("chrome")]


def _raise_yt_error(link: SourceLink, error: Exception, authenticated: bool) -> None:
    raw_detail = str(error).lower()
    detail = _safe_detail(error)
    story = "/stories/" in link.url
    if is_proxy_connection_error(error, link.url):
        code = "NETWORK_PROXY_UNAVAILABLE"
    elif "unsupported url" in raw_detail:
        code = "UNSUPPORTED_PLATFORM"
    elif any(term in raw_detail for term in ("log in", "login.php", "cookies", "authentication")):
        # A login challenge does not prove that a story expired. The stored
        # session may have been revoked or lack access to that account.
        code = "AUTH_REQUIRED"
    elif "not available" in raw_detail or "no longer" in raw_detail or "removed" in raw_detail:
        code = "MEDIA_UNAVAILABLE"
    else:
        code = "EXTRACTION_FAILED"
    logger.warning("platform=%s stage=yt_dlp error=%s story=%s cookies=%s details=%s", link.platform, code, story, authenticated, detail)
    raise ExtractionError("I couldn't get media from that public link right now", code) from error


def _yt_dlp_download(link: SourceLink, directory: Path, stopped, metadata: dict | None = None) -> tuple[list[Path], str]:
    def progress(event: dict) -> None:
        if stopped.is_set():
            raise InterruptedError("Stopped")
        if event.get("downloaded_bytes", 0) > VIDEO_LIMIT:
            raise ExtractionError("This video is too large for Telegram")

    job_cookie = _job_cookie_file(link.platform, directory)
    authenticated = bool(job_cookie)
    facebook_story = link.platform == "Facebook" and "/stories/" in link.url
    info = None
    last_error = None
    for target in _impersonate_targets(link.platform):
        options = {
            "quiet": True, "no_warnings": True, "noprogress": True,
            "noplaylist": not facebook_story,
            "format": _tiktok_format() if link.platform == "TikTok" else _format_order(),
            "merge_output_format": "mp4",
            "outtmpl": str(directory / ("media-%(id)s.%(ext)s" if facebook_story else "video.%(ext)s")),
            "max_filesize": VIDEO_LIMIT, "socket_timeout": 25, "retries": 2,
            "progress_hooks": [progress], "logger": _YtLogger(),
        }
        if target is not None:
            options["impersonate"] = target
        if link.platform == "Instagram":
            options["http_headers"] = {
                "User-Agent": BROWSER_UA,
                "Referer": "https://www.instagram.com/",
            }
        if job_cookie:
            options["cookiefile"] = str(job_cookie)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(link.url, download=True)
            break
        except yt_dlp.utils.DownloadError as error:
            if stopped.is_set():
                raise InterruptedError("Stopped") from error
            last_error = error
            if is_proxy_connection_error(error, link.url):
                _raise_yt_error(link, error, authenticated)
            if link.platform == "Facebook" and "cannot parse data" in str(error).lower():
                logger.info("platform=Facebook stage=yt_dlp error=PARSE impersonate=%s", target)
                continue
            if link.platform == "Instagram" and target is not None:
                logger.info("platform=Instagram stage=yt_dlp error=IMPERSONATE impersonate=%s", target)
                continue
            _raise_yt_error(link, error, authenticated)
    else:
        if last_error is not None:
            _raise_yt_error(link, last_error, authenticated)
        raise ExtractionError("I couldn't get media from that public link right now")
    if stopped.is_set():
        raise InterruptedError("Stopped")
    title = info.get("title", "") if isinstance(info, dict) else ""
    if metadata is not None and isinstance(info, dict) and not info.get("requested_formats") and info.get("ext") == "mp4" and info.get("protocol") in {"http", "https"}:
        direct = info.get("url", "")
        if isinstance(direct, str) and direct.startswith("https://"):
            metadata["direct_url"] = direct
    suffixes = {".mp4", ".mov", ".mkv", ".webm"}
    if facebook_story:
        suffixes.update({".jpg", ".jpeg", ".png", ".webp"})
    files = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in suffixes]
    return files[:10], title if isinstance(title, str) else ""


def _gallery_urls(link: SourceLink, stopped, limit: int = 20) -> list[str]:
    try:
        photo_page = link.platform == "Facebook" and urlsplit(link.url).path == "/photo.php"
        command = ["gallery-dl", "--no-input", "--get-urls", "--range", "1-1" if photo_page else f"1-{limit}"]
        gallery_url = link.url
        parts = urlsplit(link.url)
        if photo_page:
            query = parse_qs(parts.query)
            photo_id = query.get("fbid", [""])[0]
            album = query.get("set", [""])[0]
            if photo_id.isdecimal() and album.startswith(("a.", "pb.")):
                # gallery-dl's photo.php extractor currently loses the album
                # id and requests /media/set/?set= (404). Its album extractor
                # can select the exact photo by id.
                gallery_url = f"https://www.facebook.com/media/set/?set={album}"
                command.extend(["--filter", f"id == '{photo_id}'"])
        if link.platform == "Instagram":
            command.extend(["--user-agent", BROWSER_UA])
        source_cookie = cookie_file(link.platform)
        if source_cookie:
            command.extend(["--cookies", str(source_cookie)])
        command.append(gallery_url)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        elapsed = 0.0
        while True:
            if stopped.is_set():
                process.kill()
                process.communicate()
                raise InterruptedError("Stopped")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                elapsed += 0.2
                if elapsed >= 45:
                    process.kill()
                    process.communicate()
                    logger.warning("platform=%s stage=gallery error=TIMEOUT", link.platform)
                    return []
    except OSError:
        return []
    if process.returncode != 0:
        logger.info("platform=%s stage=gallery error=EXTRACTION_FAILED details=%s", link.platform, _safe_detail(stderr))
        return []
    urls = []
    ytdl_primary = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("| "):
            # gallery-dl can offer a direct CDN fallback for a ytdl: story
            # video. Its normal --get-urls output prefixes that URL with "| ".
            if ytdl_primary and line[2:].startswith("https://"):
                urls.append(line[2:])
        else:
            ytdl_primary = line.startswith("ytdl:")
            if line.startswith("https://"):
                urls.append(line)
    return list(dict.fromkeys(urls))[:limit]


def _facebook_post_photo_urls(post_id: str, stopped) -> list[str]:
    """Read photos attached to one post, stopping at the next post's photos.

    Facebook's pcb album viewer sometimes keeps walking older photos from the
    same account. Those entries retain the requested set_id, but their posting
    timestamp changes. Photos in one multi-image post share a timestamp.
    """
    command = [
        "gallery-dl", "--no-input", "--dump-json", "--range", "1-5",
    ]
    source_cookie = cookie_file("Facebook")
    if source_cookie:
        command.extend(["--cookies", str(source_cookie)])
    command.append(f"https://www.facebook.com/media/set/?set=pcb.{post_id}")
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        elapsed = 0.0
        while True:
            if stopped.is_set():
                process.kill()
                process.communicate()
                raise InterruptedError("Stopped")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                elapsed += 0.2
                if elapsed >= 45:
                    process.kill()
                    process.communicate()
                    logger.warning("platform=Facebook stage=post_photos error=TIMEOUT")
                    return []
    except OSError:
        return []
    if process.returncode != 0:
        logger.info("platform=Facebook stage=post_photos error=EXTRACTION_FAILED details=%s", _safe_detail(stderr))
        return []
    try:
        entries = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    urls = []
    first_date = None
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 2 or entry[0] != 2 or not isinstance(entry[1], dict):
            continue
        photo = entry[1]
        date = photo.get("date")
        if not isinstance(date, str) or not date:
            break
        if first_date is None:
            first_date = date
        elif date != first_date:
            break
        url = photo.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            urls.append(url)
    return list(dict.fromkeys(urls))


def _instagram_media(link: SourceLink) -> list[tuple[str, str]]:
    source = InstagramSource()
    parsed = parse_instagram_url(link.url)
    result = []
    for group in source.media(parsed):
        result.extend((item.url, item.kind) for item in group)
        if len(result) >= 20:
            break
    return result[:20]


async def _download_direct(urls: list[tuple[str, str | None]], link: SourceLink, directory: Path, stopped, title: str = "") -> list[MediaAsset]:
    async def fetch_one(index: int, url: str, expected: str | None, limit: int, client: httpx.AsyncClient) -> MediaAsset | None:
        if urlsplit(url).path.lower().endswith((".mp3", ".m4a", ".aac", ".ogg", ".opus")):
            return None
        path = directory / f"item-{index}.bin"
        try:
            content_type = await download_http(url, path, limit, stopped, client=client)
            if content_type.startswith("audio/"):
                return None
            path = label_media_path(path, content_type)
            kind = expected
            if kind not in {"photo", "video"}:
                kind = "photo" if content_type.startswith("image/") or urlsplit(url).path.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else "video"
            # Image decoders inspect the file content, regardless of its temporary name.
            kind, ready, still = await asyncio.to_thread(finalize_media, path, kind, stopped)
            remote_limit = 5 * 1024 * 1024 if kind == "photo" else 20 * 1024 * 1024
            # Telegram can fetch these itself on the hosted API. That path
            # recompresses photos and often transcodes video, so the home
            # server always uploads the bytes we already downloaded. Live
            # photos cannot be sent by URL at all.
            fetchable = {"image/jpeg", "image/png", "image/webp", "video/mp4"}
            remote_url = ""
            if kind != "live_photo" and not local_bot_api() and ready == path and path.stat().st_size <= remote_limit and content_type in fetchable:
                remote_url = url
            asset = MediaAsset(kind, ready, title, link.platform, link.url, remote_url, still)
            await asset_ready(asset)
            return asset
        except (httpx.HTTPError, MediaProcessingError, ValueError) as error:
            if isinstance(error, httpx.HTTPStatusError):
                logger.warning("Skipping unavailable %s item: HTTP %d host=%s", link.platform, error.response.status_code, urlsplit(url).hostname)
            else:
                logger.warning("Skipping unavailable %s item: %s", link.platform, type(error).__name__)
            return None
        finally:
            if path.exists() and path.name.startswith("item-") and path != locals().get("ready"):
                path.unlink(missing_ok=True)

    assets = []
    headers = {}
    if link.platform in {"Instagram", "TikTok"}:
        headers["Referer"] = f"https://www.{link.platform.lower()}.com/"
    if link.platform == "Instagram":
        headers["User-Agent"] = BROWSER_UA
    batch_size = 6 if link.platform == "TikTok" else 3
    async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False, headers=headers) as client:
        for start in range(0, min(len(urls), 20), batch_size):
            if stopped.is_set():
                raise InterruptedError("Stopped")
            batch = urls[start:start + batch_size]
            remaining = 160 * 1024 * 1024 - sum(asset.path.stat().st_size for asset in assets)
            if remaining <= 0:
                break
            limit = min(VIDEO_LIMIT, remaining // max(len(batch), 1))
            results = await asyncio.gather(
                *(fetch_one(start + offset, url, expected, limit, client) for offset, (url, expected) in enumerate(batch)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
                if result is not None:
                    assets.append(result)
    return assets


# Telegram caps a media album at 10 items, so a profile grid is trimmed to that.
PROFILE_ITEM_LIMIT = 10
# gallery-dl resolves an account URL only to its listing page; the real photo
# and video URLs live under these per-platform media tabs.
PROFILE_TABS = {"Instagram": "posts", "Facebook": "photos"}


async def _profile_media(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    """Enumerate a public profile grid and deliver its most recent items.

    gallery-dl walks the media tab rather than the account root, which covers
    photos, carousels and videos in one pass. Private or deleted accounts yield
    no URLs, and we report that instead of guessing at another reason.
    """
    tab = PROFILE_TABS.get(link.platform)
    listing = SourceLink(link.platform, f"{link.url.rstrip('/')}/{tab}/" if tab else link.url, "media")
    urls = await asyncio.to_thread(_gallery_urls, listing, stopped, PROFILE_ITEM_LIMIT)
    if not urls:
        raise ExtractionError("I couldn't read that profile; it may be private", "MEDIA_UNAVAILABLE")
    # ``link`` (not ``listing``) is passed on so captions name the profile.
    assets = await _download_direct([(url, None) for url in urls[:PROFILE_ITEM_LIMIT]], link, directory, stopped)
    if not assets:
        raise ExtractionError("That profile had no media I could download", "EXTRACTION_FAILED")
    return assets[:PROFILE_ITEM_LIMIT]


async def _yt_dlp_assets(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    metadata = {}
    files, title = await asyncio.to_thread(_yt_dlp_download, link, directory, stopped, metadata)
    assets = []
    for path in files:
        try:
            guessed = "photo" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} else "video"
            kind, ready, still = await asyncio.to_thread(finalize_media, path, guessed, stopped)
            remote = metadata.get("direct_url", "") if kind != "live_photo" and len(files) == 1 and ready == path and path.stat().st_size <= 20 * 1024 * 1024 else ""
            if remote:
                try:
                    await asyncio.to_thread(validate_public_url, remote)
                except ValueError:
                    remote = ""
            asset = MediaAsset(kind, ready, title, link.platform, link.url, remote, still)
            await asset_ready(asset)
            assets.append(asset)
        except MediaProcessingError:
            continue
    return assets


async def _extract_instagram(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    """Resolve Instagram posts, reels, and stories.

    Public posts and reels use instaloader's GraphQL (PolarisPostRootQuery)
    without the Netscape cookie jar. A challenged sessionid makes Instagram
    401 that same query. Stories still try gallery-dl, then yt-dlp, then Nimbus.
    """
    try:
        parsed = parse_instagram_url(link.url)
    except ValueError:
        parsed = None
    if parsed and parsed.kind == "post":
        try:
            direct = await asyncio.to_thread(_instagram_media, link)
            if direct:
                assets = await _download_direct(direct, link, directory, stopped)
                if assets:
                    return assets
        except InterruptedError:
            raise
        except Exception as error:
            logger.info("platform=Instagram stage=instaloader error=%s details=%s", type(error).__name__, _safe_detail(error))

    urls = await asyncio.to_thread(_gallery_urls, link, stopped)
    if urls:
        assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
        if assets:
            return assets

    video_error = None
    try:
        assets = await _yt_dlp_assets(link, directory, stopped)
        if assets:
            return assets
    except (ExtractionError, MediaProcessingError) as error:
        video_error = error if isinstance(error, ExtractionError) else None
        logger.info("platform=Instagram stage=video error=%s", getattr(error, "code", None) or type(error).__name__)

    assets = await nimbus_assets(link, directory, stopped)
    if assets:
        return assets
    if video_error and video_error.code in {"AUTH_REQUIRED", "NETWORK_PROXY_UNAVAILABLE"}:
        message = "Instagram requires a valid login to view this story" if video_error.code == "AUTH_REQUIRED" else "The configured network proxy is unreachable"
        raise ExtractionError(message, video_error.code) from video_error
    raise ExtractionError("I couldn't find public media I can send from that link 😕")


async def _download_tiktok_live_photos(
    pairs: list[tuple[str, str | None]], link: SourceLink, directory: Path, stopped,
) -> list[MediaAsset]:
    assets = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25, connect=8), follow_redirects=False,
        headers={"Referer": "https://www.tiktok.com/"},
    ) as client:
        for index, (image_url, clip_url) in enumerate(pairs):
            if stopped.is_set():
                raise InterruptedError("Stopped")
            still = None
            image_path = directory / f"live-{index}-photo.bin"
            try:
                content_type = await download_http(image_url, image_path, PHOTO_LIMIT, stopped, client=client)
                image_path = label_media_path(image_path, content_type)
                _, still, _ = await asyncio.to_thread(finalize_media, image_path, "photo", stopped)
            except (httpx.HTTPError, MediaProcessingError, ValueError) as error:
                logger.info("platform=TikTok stage=live_photo_image error=%s", type(error).__name__)

            clip = None
            generated_still = None
            if clip_url:
                clip_path = directory / f"live-{index}-clip.bin"
                try:
                    content_type = await download_http(clip_url, clip_path, 10 * 1024 * 1024, stopped, client=client)
                    clip_path = label_media_path(clip_path, content_type)
                    kind, ready, generated_still = await asyncio.to_thread(
                        finalize_media, clip_path, "video", stopped, live=True,
                    )
                    if kind == "live_photo":
                        clip = ready
                except (httpx.HTTPError, MediaProcessingError, ValueError) as error:
                    logger.info("platform=TikTok stage=live_photo_clip error=%s", type(error).__name__)

            if clip and (still or generated_still):
                asset = MediaAsset("live_photo", clip, "", link.platform, link.url, photo_path=still or generated_still)
            elif still:
                asset = MediaAsset("photo", still, "", link.platform, link.url)
            else:
                continue
            assets.append(asset)
            await asset_ready(asset)
    return assets


async def _extract_tiktok(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    """Prefer tikwm (one JSON call, no ffmpeg merge) then the slower extractors."""
    path = urlsplit(link.url).path
    try:
        if "/photo/" in path:
            pairs = await tiktok_live_photo_pairs(link)
            if pairs:
                assets = await _download_tiktok_live_photos(pairs, link, directory, stopped)
                if assets:
                    return assets
        urls = await tiktok_media_urls(link)
        if urls:
            if all(kind == "video" for _, kind in urls):
                for candidate in urls:
                    assets = await _download_direct([candidate], link, directory, stopped)
                    if assets:
                        return assets
            else:
                assets = await _download_direct(urls, link, directory, stopped)
                if assets:
                    return assets
    except InterruptedError:
        raise
    except Exception as error:
        logger.info("platform=TikTok stage=tikwm error=%s", type(error).__name__)

    if "/photo/" in path:
        urls = await asyncio.to_thread(_gallery_urls, link, stopped)
        if urls:
            assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
            if assets:
                return assets
    video_error = None
    try:
        assets = await _yt_dlp_assets(link, directory, stopped)
        if assets:
            return assets
    except (ExtractionError, MediaProcessingError) as error:
        video_error = error if isinstance(error, ExtractionError) else None
        logger.info("platform=TikTok stage=video error=%s", getattr(error, "code", None) or type(error).__name__)
    if "/photo/" not in path:
        urls = await asyncio.to_thread(_gallery_urls, link, stopped)
        if urls:
            assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
            if assets:
                return assets
    assets = await nimbus_assets(link, directory, stopped)
    if assets:
        return assets
    if video_error and video_error.code == "NETWORK_PROXY_UNAVAILABLE":
        raise video_error
    raise ExtractionError("I couldn't find public media I can send from that link 😕")


async def extract_media(link: SourceLink, directory: Path, stopped) -> list[MediaAsset]:
    if stopped.is_set():
        raise InterruptedError("Stopped")
    if link.kind == "profile":
        return await _profile_media(link, directory, stopped)
    # Telegram posts are only reachable over MTProto with the owner's session;
    # no web extractor can read them, so nothing else is attempted.
    if link.platform == "Telegram":
        return await telegram_assets(link, directory, stopped)
    if link.platform == "Instagram":
        return await _extract_instagram(link, directory, stopped)
    if link.platform == "TikTok":
        return await _extract_tiktok(link, directory, stopped)
    path = urlsplit(link.url).path
    if link.platform == "Facebook" and "/stories/" in link.url:
        if await _facebook_story_requires_login(link):
            raise ExtractionError("Facebook asked for a login to view this story", "AUTH_REQUIRED")
        # Try yt-dlp with cookies + impersonation; official yt-dlp doesn't fully
        # support stories yet (issue #5763) but newer versions + curl_cffi may
        # handle them. If it fails, fall through to the normal pipeline below.
    if link.platform == "Facebook" and path == "/photo.php":
        urls = await asyncio.to_thread(_gallery_urls, link, stopped)
        if urls:
            assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
            if assets:
                return assets
        raise ExtractionError("I couldn't get that Facebook photo right now", "MEDIA_UNAVAILABLE")
    if link.platform == "Facebook" and path in {"/story.php", "/permalink.php"}:
        post_id = parse_qs(urlsplit(link.url).query).get("story_fbid", [""])[0]
        if post_id.isascii() and post_id.isdecimal():
            # Facebook's legacy post URL can contain unrelated video preloads,
            # including ads. Read the post's photo group before trying yt-dlp.
            urls = await asyncio.to_thread(_facebook_post_photo_urls, post_id, stopped)
            if urls:
                assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
                if assets:
                    return assets
    # gallery-dl first for X. TikTok and Instagram have their own resolvers.
    gallery_first = link.platform == "X"
    if gallery_first:
        urls = await asyncio.to_thread(_gallery_urls, link, stopped)
        if urls:
            assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
            if assets:
                return assets

    video_error = None
    try:
        assets = await _yt_dlp_assets(link, directory, stopped)
        if assets:
            return assets
    except (ExtractionError, MediaProcessingError) as error:
        video_error = error if isinstance(error, ExtractionError) else None
        code = getattr(error, "code", None)
        logger.info("platform=%s stage=video error=%s", link.platform, code or type(error).__name__)
        if link.platform == "Facebook" and "/stories/" in link.url and code in {"AUTH_REQUIRED", "UNSUPPORTED_PLATFORM"}:
            # A Facebook story that needs a login, or whose URL shape no extractor
            # recognises, will not be rescued by gallery-dl, Nimbus or Cobalt.
            raise

    if not gallery_first:
        urls = await asyncio.to_thread(_gallery_urls, link, stopped)
        if urls:
            assets = await _download_direct([(url, None) for url in urls], link, directory, stopped)
            if assets:
                return assets
    assets = await nimbus_assets(link, directory, stopped)
    if assets:
        return assets
    assets = await cobalt_assets(link, directory, stopped)
    if assets:
        return assets
    if video_error and video_error.code == "NETWORK_PROXY_UNAVAILABLE":
        raise video_error
    raise ExtractionError("I couldn't find public media I can send from that link 😕")
