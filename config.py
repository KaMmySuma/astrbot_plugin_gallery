from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

PROXY_ENV_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


def resolve_proxy(configured: str) -> str | None:
    """Resolve the proxy URL used for Danbooru API and image requests.

    Args:
        configured: Proxy value from the plugin configuration.

    Returns:
        The configured proxy when present, otherwise the first conventional
        proxy environment variable. ``None`` disables proxying.

    The HTTP clients run with ``trust_env=False`` so an inherited environment
    cannot break them silently; this helper restores the usual proxy variables
    as an explicit fallback.
    """
    if configured.strip():
        return configured.strip()
    for name in PROXY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value
    return None


def parse_cookie_header(value: str) -> dict[str, str]:
    """Parse a browser cookie string into a mapping.

    Args:
        value: Cookie header text such as ``cf_clearance=abc; __cf_bm=def``.

    Returns:
        Cookie name/value pairs; an empty mapping when nothing usable is found.
    """
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        name, separator, cookie_value = part.partition("=")
        name = name.strip()
        if separator and name:
            cookies[name] = cookie_value.strip()
    return cookies


@dataclass(frozen=True, slots=True)
class GalleryConfig:
    """Validated runtime configuration for the gallery."""

    auto_update_enabled: bool = True
    update_interval_minutes: int = 60
    update_count: int = 20
    max_images: int = 10000
    download_concurrency: int = 2
    proxy: str = ""
    cookie: str = ""
    login: str = ""
    api_key: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> GalleryConfig:
        """Build a validated configuration from AstrBot's plugin config.

        Args:
            raw: Configuration dictionary supplied by AstrBot.

        Returns:
            A configuration with safe defaults for invalid values.
        """
        raw = raw or {}

        def positive_int(name: str, default: int, maximum: int | None = None) -> int:
            value = raw.get(name, default)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                return default
            if maximum is not None and value > maximum:
                return default
            return value

        return cls(
            auto_update_enabled=raw.get("auto_update_enabled", True)
            if isinstance(raw.get("auto_update_enabled", True), bool)
            else True,
            update_interval_minutes=positive_int("update_interval_minutes", 60),
            update_count=positive_int("update_count", 20, 200),
            max_images=positive_int("max_images", 10000),
            download_concurrency=positive_int("download_concurrency", 2),
            proxy=raw.get("proxy", "").strip()
            if isinstance(raw.get("proxy", ""), str)
            else "",
            cookie=raw.get("cookie", "").strip()
            if isinstance(raw.get("cookie", ""), str)
            else "",
            login=raw.get("login", "").strip()
            if isinstance(raw.get("login", ""), str)
            else "",
            api_key=raw.get("api_key", "").strip()
            if isinstance(raw.get("api_key", ""), str)
            else "",
        )
