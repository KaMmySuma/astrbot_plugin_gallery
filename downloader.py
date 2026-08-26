from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from curl_cffi.requests import AsyncSession, RequestsError

MAX_IMAGE_SIZE = 20 * 1024 * 1024
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
ALLOWED_EXTENSIONS = set(CONTENT_TYPE_EXTENSIONS.values())


class ImageDownloader:
    """Download validated Danbooru images into the unified images directory."""

    def __init__(self, client: httpx.AsyncClient | AsyncSession) -> None:
        self.client = client

    async def download(
        self, url: str, post_id: int, target_dir: Path, file_ext: str = ""
    ) -> Path:
        """Download one image using a post-id filename and atomic move.

        Args:
            url: Remote image URL.
            post_id: Unique Danbooru post identifier.
            target_dir: Unified image directory.
            file_ext: Extension reported by Danbooru, used as a fallback.

        Returns:
            Final downloaded image path.

        Raises:
            ValueError: If the file format or size is invalid.
            httpx.HTTPError: If the HTTP request fails.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        temporary = target_dir / f".gallery-{uuid.uuid4().hex}.tmp"
        try:
            async with self.client.stream("GET", url) as response:
                if isinstance(response, httpx.Response):
                    response.raise_for_status()
                elif response.status_code >= 400:
                    request = httpx.Request("GET", url)
                    raise httpx.HTTPStatusError(
                        f"Client error '{response.status_code}' for url '{url}'",
                        request=request,
                        response=httpx.Response(response.status_code, request=request),
                    )
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].lower()
                )
                extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
                if extension is None:
                    extension = (
                        Path(file_ext).suffix.lower()
                        or Path(url.split("?", 1)[0]).suffix.lower()
                    )
                if extension == ".jpeg":
                    extension = ".jpg"
                if extension not in ALLOWED_EXTENSIONS:
                    raise ValueError(
                        f"unsupported image format: {content_type or extension}"
                    )
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_IMAGE_SIZE:
                    raise ValueError("image exceeds 20 MB limit")
                size = 0
                with temporary.open("wb") as file:
                    chunks = (
                        response.aiter_bytes(1024 * 256)
                        if isinstance(response, httpx.Response)
                        else response.aiter_content(1024 * 256)
                    )
                    async for chunk in chunks:
                        size += len(chunk)
                        if size > MAX_IMAGE_SIZE:
                            raise ValueError("image exceeds 20 MB limit")
                        file.write(chunk)
            final_path = target_dir / f"{post_id}{extension}"
            temporary.replace(final_path)
            print(f"更新图片：{final_path}")
            return final_path
        except RequestsError as exc:
            temporary.unlink(missing_ok=True)
            raise httpx.RequestError(
                str(exc), request=httpx.Request("GET", url)
            ) from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
