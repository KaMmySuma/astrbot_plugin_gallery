from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

DANBOORU_POSTS_URL = "https://danbooru.donmai.us/posts.json"
DANBOORU_PROXY = "http://127.0.0.1:7890"
DANBOORU_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "referer": "https://danbooru.donmai.us/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}
DANBOORU_IMAGE_HEADERS = {
    "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "referer": "https://danbooru.donmai.us/",
    "user-agent": DANBOORU_HEADERS["user-agent"],
}
CORE_TAG = "angelina_(arknights)"
DANBOORU_CURSOR_FILENAME = "danbooru_cursor.json"


def load_danbooru_cursor(data_dir: Path) -> dict[str, Any]:
    """Load the persisted historical cursor.

    Args:
        data_dir: Plugin data directory containing the cursor file.

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
    cursor = payload.get("cursor")
    if not isinstance(cursor, str) or not cursor.startswith("b"):
        cursor = None
    return {"cursor": cursor, "exhausted": payload.get("exhausted") is True}


def save_danbooru_cursor(data_dir: Path, cursor: str | None, exhausted: bool) -> None:
    """Persist the historical cursor atomically.

    Args:
        data_dir: Plugin data directory in which to store the cursor file.
        cursor: Danbooru ``b<post_id>`` cursor for the next history request.
        exhausted: Whether the historical result set has been exhausted.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / DANBOORU_CURSOR_FILENAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {"cursor": cursor, "exhausted": exhausted},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


async def fetch_danbooru_images(
    count: int,
    client: httpx.AsyncClient | None = None,
    page: int | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch Safe Angelina posts and normalize complete Danbooru metadata.

    Args:
        count: Requested number of posts; each API request still fetches 200.
        client: Optional shared HTTP client owned by the plugin.
        page: Optional Danbooru page or ``b<post_id>`` cursor used to walk
            backwards through history.

    Returns:
        Normalized post dictionaries with URL, hash, extension, timestamps and tags.

    Raises:
        httpx.HTTPError: If Danbooru cannot be reached or returns an HTTP error.
        ValueError: If Danbooru returns invalid JSON.
    """
    if count < 1:
        return []
    owns_client = client is None
    request_client = client or httpx.AsyncClient(
        http2=True,
        proxy=DANBOORU_PROXY,
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        trust_env=False,
        headers=DANBOORU_HEADERS,
    )
    try:
        params: dict[str, Any] = {
            "tags": f"{CORE_TAG} rating:s",
            "limit": 200,
            "search[order]": "id_desc",
        }
        if (isinstance(page, int) and page > 0) or (isinstance(page, str) and page):
            params["page"] = page
        response = await request_client.get(DANBOORU_POSTS_URL, params=params)
        if response.headers.get("cf-mitigated") == "challenge":
            raise ValueError("Danbooru returned a Cloudflare challenge")
        if response.status_code == 410:
            return []
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Danbooru returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError("Danbooru response is not a list")
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
            await request_client.aclose()
