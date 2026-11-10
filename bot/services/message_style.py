"""Short, consistent Telegram messages using free Unicode emoji.

Telegram renders these without a custom emoji entity or extra assets. A big
standard reaction supplies the animation when a download succeeds.
"""

from urllib.parse import urlsplit

from bot.services.url_detector import SourceLink


CHECKING = "🔎 Checking link…"
QUEUED = "🕒 Queued. I'll start it when your turn comes."
UPLOADING = "📤 Uploading media…"
COMPLETE = "🎉 Download complete."
UNSUPPORTED = "🔗 I don't support that URL."


def fetching(links: list[SourceLink]) -> str:
    if len(links) != 1:
        return "⬇️ Fetching media…"
    link = links[0]
    path = urlsplit(link.url).path.lower()
    if link.kind == "highlight":
        kind = "Highlight"
    elif "/stories/" in path:
        kind = "Story"
    elif "/reel" in path:
        kind = "Reel"
    elif "/shorts/" in path:
        kind = "Short"
    elif "/photo/" in path or path == "/photo.php":
        kind = "photos"
    elif "/video" in path or link.platform == "YouTube":
        kind = "video"
    else:
        kind = "post"
    return f"⬇️ Fetching {link.platform} {kind}…"


def extraction_error(code: str) -> str:
    return {
        "AUTH_REQUIRED": "🔒 This media needs a login or is private.",
        "MEDIA_UNAVAILABLE": "🚫 Media may be private, removed, or unavailable.",
        "UNSUPPORTED_PLATFORM": UNSUPPORTED,
        "NETWORK_PROXY_UNAVAILABLE": "🛠️ Network connection is unavailable.",
        "DOWNLOAD_FAILED": "🌐 Couldn't fetch this media. Try again.",
        "TELEGRAM_UPLOAD_FAILED": "📤 Telegram couldn't accept the upload.",
    }.get(code, "❌ Couldn't download this media.")
