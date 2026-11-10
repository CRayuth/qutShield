"""Check and prepare media for direct Telegram playback."""

import json
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from bot.services.downloader import PHOTO_LIMIT, VIDEO_LIMIT


class MediaProcessingError(ValueError):
    pass


def label_media_path(path: Path, content_type: str) -> Path:
    """Give verified downloads their real extension to avoid needless conversion."""
    suffix = {"image/jpeg": ".jpg", "video/mp4": ".mp4"}.get(content_type)
    if suffix and path.suffix.lower() != suffix:
        target = path.with_suffix(suffix)
        path.rename(target)
        return target
    return path


def prepare_photo(path: Path) -> Path:
    try:
        with Image.open(path) as image:
            image.load()
            if image.width + image.height > 10000 or max(image.width, image.height) / max(1, min(image.width, image.height)) > 20:
                raise MediaProcessingError("This image does not fit Telegram's photo limits")
            output = path.with_suffix(".jpg")
            if path.suffix.lower() in {".jpg", ".jpeg"} and image.format == "JPEG" and image.mode == "RGB":
                output = path
            else:
                image.convert("RGB").save(output, "JPEG", quality=92)
    except (OSError, UnidentifiedImageError) as error:
        raise MediaProcessingError("I couldn't read that image") from error
    if output.stat().st_size > PHOTO_LIMIT:
        raise MediaProcessingError("This image is too large for Telegram")
    return output


LIVE_PHOTO_MAX_SECONDS = 10
LIVE_PHOTO_MAX_BYTES = 10 * 1024 * 1024


def probe_video(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,duration",
             "-show_entries", "format=duration,format_name", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=True,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise MediaProcessingError("I couldn't read that video") from error
    if not any(stream.get("codec_type") == "video" for stream in data.get("streams", [])):
        raise MediaProcessingError("That link did not contain a video")
    return data


def video_duration(data: dict) -> float:
    raw = (data.get("format") or {}).get("duration")
    if raw in {None, "N/A", ""}:
        for stream in data.get("streams") or []:
            if stream.get("codec_type") == "video" and stream.get("duration") not in {None, "N/A", ""}:
                raw = stream["duration"]
                break
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def is_live_photo_clip(path: Path, data: dict | None = None, *, allow_audio: bool = False) -> bool:
    """True for a short motion clip that Telegram can send as a live photo.

    Live photos are a still + a video (≤10 s, ≤10 MB). Clips with an audio
    track are treated as real videos (TikTok, Reels) unless the source already
    marked them as live (Telegram ``video_sizes``).
    """
    try:
        if path.stat().st_size > LIVE_PHOTO_MAX_BYTES:
            return False
    except OSError:
        return False
    data = data or probe_video(path)
    if video_duration(data) <= 0 or video_duration(data) > LIVE_PHOTO_MAX_SECONDS:
        return False
    streams = data.get("streams") or []
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return has_video and (allow_audio or not has_audio)


def extract_still(path: Path) -> Path:
    output = path.with_name(f"{path.stem}-still.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "0", "-i", str(path), "-frames:v", "1", "-q:v", "2", str(output)],
            check=True, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MediaProcessingError("I couldn't read that image") from error
    if not output.exists():
        raise MediaProcessingError("I couldn't read that image")
    return prepare_photo(output)


def finalize_media(path: Path, kind: str, stopped=None, *, live: bool = False) -> tuple[str, Path, Path | None]:
    """Return (kind, primary path, optional still) after Telegram-ready prep."""
    if kind == "photo" and not live:
        return "photo", prepare_photo(path), None
    video = prepare_video(path, stopped)
    data = probe_video(video)
    if is_live_photo_clip(video, data, allow_audio=live):
        return "live_photo", video, extract_still(video)
    return "video", video, None


def prepare_video(path: Path, stopped=None) -> Path:
    if stopped is not None and stopped.is_set():
        raise InterruptedError("Stopped")
    data = probe_video(path)
    streams = data.get("streams", [])
    video = next(stream for stream in streams if stream.get("codec_type") == "video")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    video_ok = video.get("codec_name") in {"h264", "hevc", "h265"}
    audio_ok = not audio or audio.get("codec_name") in {"aac", "mp3"}
    if video_ok and audio_ok and path.suffix.lower() == ".mp4":
        output = path
    else:
        output = path.with_name(f"{path.stem}-ready.mp4")
        copy_compatible = video_ok and audio_ok
        codecs = ["-c", "copy"] if copy_compatible else ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "192k"]
        try:
            process = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), *codecs, "-movflags", "+faststart", str(output)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            elapsed = 0.0
            while process.poll() is None:
                if stopped is not None and stopped.is_set():
                    process.kill()
                    process.wait()
                    raise InterruptedError("Stopped")
                if elapsed >= 180:
                    process.kill()
                    process.wait()
                    raise MediaProcessingError("Video processing timed out")
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    elapsed += 0.2
            if process.returncode:
                raise MediaProcessingError("I couldn't prepare that video for Telegram")
        except (OSError, subprocess.SubprocessError) as error:
            raise MediaProcessingError("I couldn't prepare that video for Telegram") from error
    if output.stat().st_size > VIDEO_LIMIT:
        raise MediaProcessingError("This video is too large for Telegram")
    if output != path:
        probe_video(output)
    return output
