"""Recognize supported links and reject unsafe redirects."""

import asyncio
import base64
import binascii
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx


URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
# Every host below maps to a canonical platform name. Redirects are only
# followed while they stay within the same platform (see ``resolve_link``).
HOSTS = {
    "facebook.com": "Facebook", "www.facebook.com": "Facebook", "m.facebook.com": "Facebook",
    "web.facebook.com": "Facebook", "mbasic.facebook.com": "Facebook", "business.facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "youtube.com": "YouTube", "www.youtube.com": "YouTube", "m.youtube.com": "YouTube", "youtu.be": "YouTube",
    "x.com": "X", "www.x.com": "X", "twitter.com": "X", "www.twitter.com": "X", "mobile.twitter.com": "X", "t.co": "X",
    "tiktok.com": "TikTok", "www.tiktok.com": "TikTok", "m.tiktok.com": "TikTok", "vm.tiktok.com": "TikTok", "vt.tiktok.com": "TikTok",
    "instagram.com": "Instagram", "www.instagram.com": "Instagram", "m.instagram.com": "Instagram",
    # Telegram links are read over MTProto by the owner's session, not scraped.
    "t.me": "Telegram", "www.t.me": "Telegram", "telegram.me": "Telegram",
}
SHORT_HOSTS = {"fb.watch", "t.co", "vm.tiktok.com", "vt.tiktok.com"}
# Apex domains used to accept intermediate redirect hops on a subdomain that is
# not itself listed in HOSTS -- TikTok short links route through t.tiktok.com,
# and an exact-host check aborted the chain before reaching the real video.
APEX_DOMAINS = {
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "x.com": "X", "twitter.com": "X", "t.co": "X",
    "tiktok.com": "TikTok", "douyin.com": "TikTok",
    "instagram.com": "Instagram",
}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{4,80}\Z")
# Accept any length of numeric id. Real ids are long, but rejecting a short one
# here produced a misleading "Unsupported link" instead of letting the
# extractor report the media as unavailable.
NUMERIC = re.compile(r"[0-9]{1,30}\Z")
FACEBOOK_SET = re.compile(r"(?:a|pb)\.[A-Za-z0-9._-]{1,100}\Z")
HANDLE = re.compile(r"[A-Za-z0-9._]{1,60}\Z")
STORY_ID = re.compile(r"[A-Za-z0-9_=:-]{10,160}\Z")
SHARE_KINDS = {"p", "v", "r", "x"}
# Handles are shorter than generic path tokens, and a bare ``/{handle}`` is a
# profile grid rather than one media item.
USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}\Z")
# Telegram usernames: letters, digits and underscore, 5-32 characters.
TG_CHANNEL = re.compile(r"[A-Za-z0-9_]{5,32}\Z")
# Single-segment t.me paths that are feature routes, never a channel name.
TELEGRAM_RESERVED = {"share", "joinchat", "addstickers", "addemoji", "proxy",
                     "setlanguage", "confirmemail", "activate", "login", "s", "c"}
# Single-segment Instagram paths that look like handles but never are.
INSTAGRAM_RESERVED = {
    "p", "reel", "reels", "stories", "s", "tv", "explore", "accounts", "direct", "about",
    "developer", "business", "legal", "shop", "directory", "suggested_feeds", "i",
    "web", "radio", "invites", "email_unsubscribe", "pb", "privacypolicy", "terms",
    "support", "help", "www", "m",
}
# Same idea for Facebook; anything that resolves to a feature page, not a person.
FACEBOOK_RESERVED = {
    "p", "reel", "reels", "stories", "watch", "videos", "video", "photos", "photo",
    "shares", "share", "groups", "pages", "events", "gaming", "marketplace", "business",
    "help", "privacy", "terms", "login", "logout", "register", "signup", "account",
    "settings", "friends", "saved", "directory", "hashtag", "live", "ads", "about",
    "policies", "community", "topics", "w", "fundraisers", "jobs", "job", "ngi", "www",
    "m", "web", "mbasic", "business",
}


def _is_handle(value: str, reserved: set[str]) -> bool:
    """True when a single path segment is a plausible profile handle."""
    if not USERNAME.fullmatch(value) or value.lower() in reserved:
        return False
    # Handles may not be dots only, nor start/end with a dot.
    return not value.startswith(".") and not value.endswith(".") and set(value) != {"."}


def _smuggled_url(segments) -> bool:
    """Detect two links pasted with no separator.

    "https://a.test/x/https://b.test/y/" parses as one URL whose path holds a
    nested scheme. Treating that as a single link silently loses the first one,
    so reject it and let candidate_urls split it instead.
    """
    return any(segment.lower().startswith(("http:", "https:")) for segment in segments)


@dataclass(frozen=True)
class SourceLink:
    platform: str
    url: str
    # ``media`` is one post/reel/photo; ``profile`` and ``highlight`` are collections.
    kind: str = "media"
    # Keep the user's share link for captions while extractors use its target.
    display_url: str = ""


def candidate_urls(text: str, hidden_urls=()) -> list[str]:
    # A nested scheme inside a match means two links were pasted with no
    # separator; splitting here keeps both instead of dropping one as a bad path.
    found = []
    for match in URL_PATTERN.finditer(text or ""):
        found.extend(part for part in re.split(r"(?=https?://)", match.group()) if part)
    found = [url.rstrip(".,!?)]}\"'") for url in found]
    found.extend(url for url in hidden_urls if isinstance(url, str))
    return list(dict.fromkeys(found))[:8]


def classify_url(value: str) -> SourceLink | None:
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower()
        # Only https URLs are accepted: this matches ``validate_public_url``
        # and prevents an attacker from downgrading a supported host to
        # cleartext via a hand-typed link.
        if parts.scheme != "https" or parts.port is not None or parts.username or parts.password or host not in HOSTS:
            return None
    except ValueError:
        return None
    path = parts.path.rstrip("/") or "/"
    segments = [segment for segment in path.split("/") if segment]
    if _smuggled_url(segments):
        return None
    platform = HOSTS[host]
    if host in SHORT_HOSTS:
        return SourceLink(platform, urlunsplit(("https", host, path, "", ""))) if segments else None
    if platform == "YouTube":
        video_id = segments[0] if host == "youtu.be" and len(segments) == 1 else None
        if path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        elif len(segments) == 2 and segments[0] in {"shorts", "live", "embed", "v"}:
            video_id = segments[1]
        if video_id and IDENTIFIER.fullmatch(video_id):
            return SourceLink(platform, f"https://www.youtube.com/watch?v={video_id}")
    elif platform == "X":
        if len(segments) >= 3 and segments[1] == "status" and NUMERIC.fullmatch(segments[2]):
            return SourceLink(platform, f"https://x.com/{segments[0]}/status/{segments[2]}")
    elif platform == "TikTok":
        # /@user/video|photo/<id> and the older /video/<id> without a handle.
        if len(segments) == 3 and segments[0].startswith("@") and segments[1] in {"video", "photo"} and NUMERIC.fullmatch(segments[2]):
            return SourceLink(platform, f"https://www.tiktok.com/{segments[0]}/{segments[1]}/{segments[2]}")
        if len(segments) == 2 and segments[0] in {"video", "photo"} and NUMERIC.fullmatch(segments[1]):
            return SourceLink(platform, f"https://www.tiktok.com/{segments[0]}/{segments[1]}")
    elif platform == "Instagram":
        if len(segments) == 2 and segments[0] in {"p", "reel", "reels"} and IDENTIFIER.fullmatch(segments[1]):
            return SourceLink(platform, f"https://www.instagram.com/{segments[0]}/{segments[1]}/")
        if len(segments) == 3 and segments[:2] == ["stories", "highlights"] and NUMERIC.fullmatch(segments[2]):
            return SourceLink(platform, f"https://www.instagram.com/stories/highlights/{segments[2]}/", "highlight")
        if len(segments) == 3 and segments[0] == "stories" and USERNAME.fullmatch(segments[1]) and NUMERIC.fullmatch(segments[2]):
            return SourceLink(platform, f"https://www.instagram.com/stories/{segments[1]}/{segments[2]}/")
        # instagram.com/{handle} – shared via the profile's "Copy Link" button.
        if len(segments) == 1 and _is_handle(segments[0], INSTAGRAM_RESERVED):
            return SourceLink(platform, f"https://www.instagram.com/{segments[0]}/", "profile")
        # /s/<code> – shared highlight. The media id rides in the query, and
        # parse_instagram_url already decodes the code, so accepting this shape
        # turns an instant "Unsupported link" into a real extraction attempt.
        if len(segments) == 2 and segments[0] == "s":
            media_id = parse_qs(parts.query).get("story_media_id", [""])[0]
            code = segments[1]
            if IDENTIFIER.fullmatch(code.rstrip("=")) and NUMERIC.fullmatch(media_id):
                # gallery-dl's highlight extractor expects the code directly
                # before the query. A slash here is parsed as part of its
                # base64 code and makes otherwise valid highlights fail.
                return SourceLink(platform, f"https://www.instagram.com/s/{code}?story_media_id={media_id}", "highlight")
            if not media_id and IDENTIFIER.fullmatch(code.rstrip("=")):
                try:
                    decoded = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)).decode("ascii")
                except (ValueError, UnicodeError, binascii.Error):
                    return None
                if decoded.startswith("highlight:") and NUMERIC.fullmatch(decoded[10:]):
                    # A mobile share of the whole highlight has no story_media_id.
                    # Turn it into the collection URL the gallery extractor reads.
                    return SourceLink(platform, f"https://www.instagram.com/stories/highlights/{decoded[10:]}/", "highlight")
    elif platform == "Telegram":
        # /c/<peer>/<post> – private channel, reachable only by the session.
        if len(segments) == 3 and segments[0] == "c" and NUMERIC.fullmatch(segments[1]) and NUMERIC.fullmatch(segments[2]):
            return SourceLink(platform, f"https://t.me/c/{segments[1]}/{segments[2]}")
        # /<channel>/<post> – public channel post.
        if len(segments) == 2 and TG_CHANNEL.fullmatch(segments[0]) and segments[0].lower() not in TELEGRAM_RESERVED and NUMERIC.fullmatch(segments[1]):
            return SourceLink(platform, f"https://t.me/{segments[0]}/{segments[1]}")
    elif platform == "Facebook":
        if path == "/photo.php":
            query = parse_qs(parts.query)
            fbid = query.get("fbid", [""])[0]
            if NUMERIC.fullmatch(fbid):
                photo_url = f"https://www.facebook.com/photo.php?fbid={fbid}"
                album = query.get("set", [""])[0]
                if FACEBOOK_SET.fullmatch(album):
                    photo_url += f"&set={album}"
                return SourceLink(platform, photo_url)
        if path in {"/permalink.php", "/story.php"}:
            query = parse_qs(parts.query)
            story = query.get("story_fbid", [""])[0]
            owner = query.get("id", [""])[0]
            if NUMERIC.fullmatch(story) and NUMERIC.fullmatch(owner):
                return SourceLink(platform, f"https://www.facebook.com{path}?story_fbid={story}&id={owner}")
        if (segments and ("reel" in segments or "videos" in segments)) or path == "/watch" and parse_qs(parts.query).get("v"):
            query = f"v={parse_qs(parts.query)['v'][0]}" if path == "/watch" else ""
            return SourceLink(platform, urlunsplit(("https", host, path, query, "")))
        # /share/{short}/ – bare alphanumeric share token
        if len(segments) == 2 and segments[0] == "share" and IDENTIFIER.fullmatch(segments[1]):
            return SourceLink(platform, f"https://www.facebook.com/share/{segments[1]}/")
        # /share/{p|v|r|x}/{short}/ – post, video, reel and generic share slugs
        if len(segments) == 3 and segments[0] == "share" and segments[1] in SHARE_KINDS and IDENTIFIER.fullmatch(segments[2]):
            return SourceLink(platform, f"https://www.facebook.com/share/{segments[1]}/{segments[2]}/")
        # /stories/{numeric_uid}/{story_id}/
        if len(segments) == 3 and segments[0] == "stories" and NUMERIC.fullmatch(segments[1]) and STORY_ID.fullmatch(segments[2]):
            return SourceLink(platform, f"https://www.facebook.com/stories/{segments[1]}/{segments[2]}/")
        # /stories/{numeric_uid}/ opens the actor's story highlight collection.
        if len(segments) == 2 and segments[0] == "stories" and NUMERIC.fullmatch(segments[1]):
            return SourceLink(platform, f"https://www.facebook.com/stories/{segments[1]}/", "highlight")
        # /{username}/posts|photos|videos/{numeric_id}/ – public timeline items
        if len(segments) == 3 and segments[1] in {"posts", "photos"} and NUMERIC.fullmatch(segments[2]) and HANDLE.fullmatch(segments[0]):
            return SourceLink(platform, f"https://www.facebook.com/{segments[0]}/{segments[1]}/{segments[2]}/")
        # facebook.com/{username} – profile grid
        if len(segments) == 1 and _is_handle(segments[0], FACEBOOK_RESERVED):
            return SourceLink(platform, f"https://www.facebook.com/{segments[0]}/", "profile")
    return None


def validate_public_url(value: str) -> None:
    """Validate a media URL before handing it to an HTTP client."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.scheme != "https" or not host or parts.port not in {None, 443} or parts.username or parts.password:
            raise ValueError
        if host.lower() in {"localhost"} or host.lower().endswith((".local", ".internal", ".localhost")):
            raise ValueError
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError
    except (OSError, ValueError) as error:
        raise ValueError("That media link is not safe to open") from error


def platform_of(host: str) -> str | None:
    """Map a host to its platform, accepting any real subdomain of the apex.

    The suffix test is anchored with a leading dot so a lookalike such as
    ``tiktok.com.evil.test`` does not inherit TikTok's trust.
    """
    host = (host or "").lower()
    if host in HOSTS:
        return HOSTS[host]
    for apex, platform in APEX_DOMAINS.items():
        if host == apex or host.endswith("." + apex):
            return platform
    return None


def terminal_link(value: str, platform: str) -> SourceLink | None:
    """Build a link for a resolved URL on a trusted platform host.

    Drops only the fragment; query parameters are kept because forms such as
    ``/watch?v=`` and ``/photo.php?fbid=`` carry the media id there.
    """
    if platform_of(urlsplit(value).hostname or "") != platform:
        return None
    scheme, netloc, path, query, _ = urlsplit(value)
    return SourceLink(platform, urlunsplit(("https", netloc, path or "/", query, "")))


async def resolve_link(value: str, client: httpx.AsyncClient) -> SourceLink | None:
    """Follow redirects for short and share links, keeping SSRF checks.

    Only URLs on an allowlisted platform host are ever returned, and every hop
    is validated before it is requested. When the chain ends on a supported
    host whose path is not in ``classify_url``'s allowlist, the *resolved* URL
    is handed back rather than the original share token: yt-dlp understands
    most platform URL shapes, but not ``/share/<token>``, so returning the
    token produced "Unsupported URL" for perfectly valid posts.
    """
    link = classify_url(value)
    if link is None:
        return None
    parts = urlsplit(link.url)
    original_host = (parts.hostname or "").lower()
    expected_platform = platform_of(original_host)
    needs_resolution = original_host in SHORT_HOSTS or parts.path.startswith("/share/")
    if not needs_resolution:
        return link

    def with_display_url(resolved: SourceLink | None) -> SourceLink | None:
        if resolved is None or expected_platform != "Facebook" or not parts.path.startswith("/share/"):
            return resolved
        return SourceLink(resolved.platform, resolved.url, resolved.kind, link.url)

    current = link.url
    for _ in range(5):
        await asyncio.to_thread(validate_public_url, current)
        try:
            async with client.stream("GET", current, follow_redirects=False, timeout=12) as response:
                status = response.status_code
                location = response.headers.get("location", "")
        except httpx.HTTPError:
            return None
        if status not in {301, 302, 303, 307, 308}:
            resolved = classify_url(current)
            if resolved is not None and resolved.platform == expected_platform:
                return with_display_url(resolved)
            return with_display_url(terminal_link(current, expected_platform))
        current = urljoin(current, location)
        # Reject redirect-hijacks that leave the source platform (SSRF guard).
        if platform_of(urlsplit(current).hostname or "") != expected_platform:
            return None
        # Facebook often redirects www -> web while keeping the same /share/
        # path. Follow that hop to the actual post, photo, or reel URL.
        if expected_platform == "Facebook" and urlsplit(current).path.startswith("/share/"):
            continue
        resolved = classify_url(current)
        if resolved is not None:
            return with_display_url(resolved)
    return with_display_url(terminal_link(current, expected_platform))
