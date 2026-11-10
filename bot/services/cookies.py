"""Locate owner-provided social cookies without exposing their contents."""

import http.cookiejar
import os
from pathlib import Path


FILENAMES = {
    "Facebook": "www.facebook.com_cookies.txt",
    "Instagram": "www.instagram.com_cookies.txt",
    "TikTok": "www.tiktok.com_cookies.txt",
    "X": "x.com_cookies.txt",
}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def cookie_file(platform: str) -> Path | None:
    directory = os.getenv("SOCIAL_COOKIE_DIR", "").strip()
    name = FILENAMES.get(platform)
    if not name:
        return None
    # Local Python runs can use the owner-provided files beside README.md.
    # Deployments keep using the explicit read-only secrets mount.
    path = (Path(directory) if directory else PROJECT_ROOT) / name
    try:
        if path.is_file() and 0 < path.stat().st_size <= 1024 * 1024:
            return path
    except OSError:
        pass
    return None


def http_cookies(platform: str) -> http.cookiejar.MozillaCookieJar | None:
    path = cookie_file(platform)
    if not path:
        return None
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=False)
    except (OSError, ValueError, http.cookiejar.LoadError):
        return None
    return jar
