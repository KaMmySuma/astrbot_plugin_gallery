from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession

from ..config import parse_cookie_header

DANBOORU_POSTS_URL = "https://danbooru.donmai.us/posts.json"
# Never send a custom user-agent here. curl_cffi impersonation ships a user
# agent that matches its TLS/HTTP2 fingerprint; overriding it with a different
# Chrome version makes Cloudflare challenge every request.
DANBOORU_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "referer": "https://danbooru.donmai.us/",
}
DANBOORU_IMAGE_HEADERS = {
    "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "referer": "https://danbooru.donmai.us/",
}
CORE_TAG = "angelina_(arknights)"
# Danbooru split the old "safe" rating into general and sensitive. Querying
# only ``rating:s`` now matches sensitive posts exclusively and silently drops
# every general post, so both ratings must be requested together.
SAFE_RATING = "rating:g,s"
DANBOORU_QUERY = f"{CORE_TAG} {SAFE_RATING}"
DANBOORU_CURSOR_FILENAME = "danbooru_cursor.json"
DANBOORU_MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
# Danbooru sits behind Cloudflare, which escalates to a managed challenge when
# one IP walks history pages back to back. Keep a minimum gap between calls.
DANBOORU_MIN_INTERVAL = 1.2
_last_request_at = 0.0


async def _throttle() -> None:
    """Space out API calls so a history backfill cannot trip the WAF."""
    global _last_request_at
    wait = DANBOORU_MIN_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        await asyncio.sleep(wait + random.uniform(0.0, 0.4))
    _last_request_at = time.monotonic()


class DanbooruError(RuntimeError):
    """Raised when Danbooru cannot be reached or answers with an error."""


class DanbooruConnectionError(DanbooruError):
    """Raised when no HTTP response could be obtained from Danbooru."""


def _auth_header(login: str, api_key: str) -> dict[str, str]:
    """Build a Danbooru HTTP Basic authorization header.

    Args:
        login: Danbooru user name.
        api_key: Danbooru API key.

    Returns:
        A header mapping, or an empty mapping when either value is missing.

    Basic auth is used instead of ``login``/``api_key`` query parameters so the
    key never lands in proxy logs or CDN access logs.
    """
    if not login or not api_key:
        return {}
    token = base64.b64encode(f"{login}:{api_key}".encode()).decode()
    return {"authorization": f"Basic {token}"}


def create_danbooru_client(
    proxy: str | None = None,
    cookie: str = "",
    login: str = "",
    api_key: str = "",
) -> AsyncSession:
    """Create a browser-impersonating client for the Danbooru API.

    Cloudflare challenges plain TLS fingerprints, so API requests reuse the
    same impersonated client type as image downloads. When the exit IP is
    flagged, a ``cf_clearance`` cookie copied from a browser that already
    solved the challenge is sent with every request.

    Args:
        proxy: Optional proxy URL; ``None`` connects directly.
        cookie: Optional browser cookie header, usually ``cf_clearance=...``.
        login: Optional Danbooru user name for authenticated requests.
        api_key: Optional Danbooru API key.

    Returns:
        A configured ``curl_cffi`` async session.
    """
    cookies = parse_cookie_header(cookie) if cookie else None
    headers = {**DANBOORU_HEADERS, **_auth_header(login, api_key)}
    return AsyncSession(
        impersonate="chrome",
        proxy=proxy or None,
        timeout=20.0,
        allow_redirects=True,
        headers=headers,
        cookies=cookies or None,
    )


async def close_danbooru_client(client: Any) -> None:
    """Close a curl_cffi or httpx client without depending on its type."""
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if asyncio.iscoroutine(result):
        await result


async def _request_posts(
    client: Any, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Request one page of posts and retry transient failures.

    Args:
        client: HTTP client exposing ``get`` with an httpx-like response.
        params: Query parameters for the posts endpoint.

    Returns:
        Raw post dictionaries returned by Danbooru.

    Raises:
        DanbooruError: On network failures or unrecoverable HTTP errors.
        ValueError: On a Cloudflare challenge or malformed JSON.
    """
    last_error: Exception = DanbooruError("Danbooru request failed")
    for attempt in range(1, DANBOORU_MAX_ATTEMPTS + 1):
        await _throttle()
        try:
            response = await client.get(DANBOORU_POSTS_URL, params=params)
        except Exception as exc:
            last_error = DanbooruConnectionError(f"{type(exc).__name__}: {exc}")
        else:
            status = int(response.status_code)
            headers = response.headers
            mitigated = headers.get("cf-mitigated") or headers.get("CF-Mitigated")
            if mitigated == "challenge":
                last_error = ValueError("Danbooru returned a Cloudflare challenge")
            elif status == 410:
                return []
            elif status in RETRYABLE_STATUS:
                last_error = DanbooruError(f"Danbooru returned HTTP {status}")
            elif status >= 400:
                raise DanbooruError(f"Danbooru returned HTTP {status}")
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ValueError("Danbooru returned invalid JSON") from exc
                if not isinstance(payload, list):
                    raise ValueError("Danbooru response is not a list")
                return payload
        if attempt < DANBOORU_MAX_ATTEMPTS:
            await asyncio.sleep(1.5 * attempt + random.random())
    raise last_error


def load_danbooru_cursor(data_dir: Path, query: str = DANBOORU_QUERY) -> dict[str, Any]:
    """Load the persisted historical cursor.

    Args:
        data_dir: Plugin data directory containing the cursor file.
        query: Tag query the cursor belongs to. A cursor written for a
            different query is discarded so the history is walked again.

    Returns:
        Cursor state, or an empty dictionary when no valid state exists.
    """
    try:
        payload = json.loads(
            (data_dir / DANBOORU_CURSOR_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("query") != query:
        return {}
    cursor = payload.get("cursor")
    if not isinstance(cursor, str) or not cursor.startswith("b"):
        cursor = None
    return {"cursor": cursor, "exhausted": payload.get("exhausted") is True}


def save_danbooru_cursor(
    data_dir: Path,
    cursor: str | None,
    exhausted: bool,
    query: str = DANBOORU_QUERY,
) -> None:
    """Persist the historical cursor atomically.

    Args:
        data_dir: Plugin data directory in which to store the cursor file.
        cursor: Danbooru ``b<post_id>`` cursor for the next history request.
        exhausted: Whether the historical result set has been exhausted.
        query: Tag query the cursor belongs to.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / DANBOORU_CURSOR_FILENAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {"cursor": cursor, "exhausted": exhausted, "query": query},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


async def fetch_danbooru_images(
    count: int,
    client: Any | None = None,
    page: int | str | None = None,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch Safe Angelina posts and normalize complete Danbooru metadata.

    Args:
        count: Requested number of posts; each API request still fetches 200.
        client: Optional shared HTTP client owned by the plugin. Both
            ``curl_cffi.requests.AsyncSession`` and ``httpx.AsyncClient`` are
            accepted.
        page: Optional Danbooru page or ``b<post_id>`` cursor used to walk
            backwards through history.
        proxy: Optional proxy URL used when creating a private HTTP client.

    Returns:
        Normalized post dictionaries with URL, hash, extension, timestamps and tags.

    Raises:
        DanbooruError: If Danbooru cannot be reached or returns an HTTP error.
        ValueError: If Danbooru returns a Cloudflare challenge or invalid JSON.
    """
    if count < 1:
        return []
    owns_client = client is None
    request_client = client or create_danbooru_client(proxy)
    try:
        params: dict[str, Any] = {
            "tags": DANBOORU_QUERY,
            "limit": 200,
            "search[order]": "id_desc",
        }
        if (isinstance(page, int) and page > 0) or (isinstance(page, str) and page):
            params["page"] = page
        payload = await _request_posts(request_client, params)
        results: list[dict[str, Any]] = []
        seen_post_ids: set[int] = set()
        for post in payload:
            if not isinstance(post, dict):
                continue
            post_id = post.get("id")
            if (
                not isinstance(post_id, int)
                or post_id in seen_post_ids
                or post.get("is_deleted")
                or post.get("is_banned")
                or post.get("is_pending")
            ):
                continue
            file_url = post.get("file_url")
            if not isinstance(file_url, str):
                variants = post.get("media_asset", {}).get("variants", [])
                if isinstance(variants, list):
                    for variant in variants:
                        if (
                            isinstance(variant, dict)
                            and variant.get("type") == "original"
                        ):
                            file_url = variant.get("url")
                            break
            if not isinstance(file_url, str) or not file_url.startswith(
                ("http://", "https://")
            ):
                continue
            tag_string = post.get("tag_string") or ""
            tags = tag_string.split() if isinstance(tag_string, str) else []
            seen_post_ids.add(post_id)
            results.append(
                {
                    "post_id": post_id,
                    "created_at": str(post.get("created_at") or ""),
                    "file_url": file_url,
                    "file_ext": str(post.get("file_ext") or ""),
                    "file_size": (
                        post.get("file_size")
                        if isinstance(post.get("file_size"), int)
                        else 0
                    ),
                    "md5": str(post.get("md5") or ""),
                    "tags": tags,
                }
            )
        return results
    finally:
        if owns_client:
            await close_danbooru_client(request_client)
