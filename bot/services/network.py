"""Diagnose the proxy used for public HTTPS media requests.

The bot's HTTP libraries already honor the standard proxy environment
variables. This module reports a broken proxy; it never silently bypasses one.
Run ``python -m bot.services.network`` on any machine running the bot.
"""

import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

import httpx


CHECK_URL = "https://www.instagram.com/"


@dataclass(frozen=True)
class NetworkCheck:
    code: str
    message: str


def configured_proxy(url: str = CHECK_URL) -> str | None:
    """Return the HTTPS proxy for a URL, honoring standard NO_PROXY rules."""
    host = urlsplit(url).hostname or ""
    if proxy_bypass_environment(host):
        return None
    proxies = getproxies_environment()
    return (proxies.get("https") or proxies.get("all") or "").strip() or None


def proxy_endpoint(url: str = CHECK_URL) -> tuple[str, int] | None:
    value = configured_proxy(url)
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname:
            return None
        return parts.hostname, parts.port or (443 if parts.scheme == "https" else 1080 if parts.scheme.startswith("socks") else 80)
    except ValueError:
        return None


def is_proxy_connection_error(error: Exception | str, url: str = CHECK_URL) -> bool:
    """Classify only errors tied to the configured proxy endpoint."""
    endpoint = proxy_endpoint(url)
    if not endpoint:
        return False
    detail = str(error).lower()
    host, port = endpoint
    host = host.lower()
    return (
        (host in detail and re.search(rf"\bport\s+{port}\b|:{port}\b", detail) is not None
         and any(term in detail for term in ("failed to connect", "connection refused", "could not connect")))
        or ("proxyerror" in detail and any(term in detail for term in ("failed", "refused", "connect")))
    )


def check_network() -> NetworkCheck:
    """Check the configured proxy, then make one ordinary HTTPS request."""
    proxy = configured_proxy(CHECK_URL)
    if proxy:
        endpoint = proxy_endpoint(CHECK_URL)
        if not endpoint:
            return NetworkCheck("INVALID_PROXY", "The HTTPS proxy setting is invalid. Check HTTPS_PROXY or ALL_PROXY.")
        try:
            with socket.create_connection(endpoint, timeout=3):
                pass
        except OSError:
            return NetworkCheck("PROXY_UNREACHABLE", "The configured HTTPS proxy is unreachable. Set a working HTTPS_PROXY or clear it in the bot's runtime.")
    try:
        with httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False) as client:
            response = client.get(CHECK_URL)
        return NetworkCheck("OK", f"HTTPS reached Instagram (HTTP {response.status_code}).")
    except httpx.HTTPError as error:
        if proxy and is_proxy_connection_error(error, CHECK_URL):
            return NetworkCheck("PROXY_UNREACHABLE", "The configured HTTPS proxy could not complete a request. Check HTTPS_PROXY or ALL_PROXY.")
        return NetworkCheck("HTTPS_UNREACHABLE", f"HTTPS request failed: {type(error).__name__}. Check DNS, firewall, and proxy settings.")
    except (ValueError, ImportError):
        return NetworkCheck("INVALID_PROXY", "The HTTPS proxy setting is unsupported by this client. Check HTTPS_PROXY or ALL_PROXY.")


if __name__ == "__main__":
    result = check_network()
    print(f"{result.code}: {result.message}")
    raise SystemExit(0 if result.code == "OK" else 1)
