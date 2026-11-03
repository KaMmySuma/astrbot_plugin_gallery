from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register

from .api.danbooru import (
    CORE_TAG,
    DANBOORU_IMAGE_HEADERS,
    DANBOORU_QUERY,
    DanbooruConnectionError,
    close_danbooru_client,
    create_danbooru_client,
    fetch_danbooru_images,
    load_danbooru_cursor,
    save_danbooru_cursor,
)
from .config import GalleryConfig, parse_cookie_header, resolve_proxy
from .downloader import ALLOWED_EXTENSIONS, MAX_IMAGE_SIZE, ImageDownloader
from .gallery import TAG_MAPPING, Gallery
from .scheduler import GalleryScheduler
from .statistics import generate_statistics_image


@register(
    "astrbot_plugin_gallery",
    "Abyss",
    "基于SQLite多标签元数据的 Safe Dance Angelina 同人图管理",
    "1.0.1",
    "没有喵",
)
class GalleryPlugin(Star):
    """AstrBot commands and lifecycle for the tagged fanart gallery."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self.config = GalleryConfig.from_dict(config)
        self.gallery = Gallery(StarTools.get_data_dir("astrbot_plugin_gallery"))
        self.http_client: AsyncSession | None = None
        self.image_client: AsyncSession | None = None
        self.downloader: ImageDownloader | None = None
        self.scheduler: GalleryScheduler | None = None
        self.proxy: str | None = None
        self._update_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize files, SQLite, shared HTTP resources, and the scheduler."""
        await self.gallery.initialize()
        proxy = resolve_proxy(self.config.proxy)
        self.proxy = proxy
        if proxy:
            self.logger.info("Danbooru requests will use proxy %s.", proxy)
        else:
            self.logger.warning(
                "No proxy configured for Danbooru requests; direct connections "
                "may be blocked. Set the plugin proxy option if updates fail."
            )
        self.http_client = create_danbooru_client(
            proxy,
            self.config.cookie,
            self.config.login,
            self.config.api_key,
        )
        self.image_client = AsyncSession(
            impersonate="chrome",
            proxy=proxy,
            timeout=20.0,
            headers=DANBOORU_IMAGE_HEADERS,
            cookies=parse_cookie_header(self.config.cookie) or None,
        )
        self.downloader = ImageDownloader(self.image_client)
        if self.config.auto_update_enabled:
            self.scheduler = GalleryScheduler(
                self.config.update_interval_minutes, self.update_gallery
            )
            self.scheduler.start()
            self.logger.info(
                "Gallery auto-update started with a %s minute interval.",
                self.config.update_interval_minutes,
            )

    async def terminate(self) -> None:
        """Stop periodic work and release database and HTTP resources."""
        if self.scheduler is not None:
            await self.scheduler.wait_stopped()
            self.scheduler = None
        if self.http_client is not None:
            await close_danbooru_client(self.http_client)
            self.http_client = None
            self.downloader = None
        if self.image_client is not None:
            await self.image_client.close()
            self.image_client = None
        await self.gallery.close()
        self.logger.info("Gallery plugin resources were released.")

    def _log_request_failure(self, exc: Exception) -> None:
        """Log a Danbooru request failure with an actionable proxy hint.

        Args:
            exc: Exception raised by the HTTP client.
        """
        self.logger.exception("Danbooru API request failed.")
        target = self.proxy or "direct connection"
        if isinstance(exc, DanbooruConnectionError):
            self.logger.error(
                "Cannot open a TCP connection via %s. Verify the proxy address "
                "is reachable from the AstrBot container: use the container "
                "gateway IP or host.docker.internal, and make the proxy listen "
                "on 0.0.0.0 instead of a single docker bridge address.",
                target,
            )
        elif "Cloudflare challenge" in str(exc):
            self.logger.error(
                "Danbooru answered with a Cloudflare challenge via %s. The exit "
                "IP is flagged: switch to another proxy node or a residential "
                "exit, then retry.",
                target,
            )

    async def update_gallery(self) -> None:
        """Fetch, download, index, and retain Safe Angelina fanart.

        The adapter always queries the configured core tag. Pagination continues
        until the requested number of new posts is collected or history is exhausted.
        """
        async with self._update_lock:
            if self.http_client is None or self.downloader is None:
                self.logger.error("Gallery HTTP resources are not initialized.")
                return
            try:
                await self.gallery.sync_local_images()
            except Exception as exc:
                self.logger.exception("Local gallery synchronization failed.")
                self.gallery.save_failure(f"{type(exc).__name__}: {exc}")
                return
            added_total = 0
            added_by_tag = dict.fromkeys(TAG_MAPPING.values(), 0)
            request_succeeded = False
            last_error: str | None = None
            pending: list[dict[str, Any]] = []
            pending_post_ids: set[int] = set()
            pending_hashes: set[str] = set()
            cursor_state = load_danbooru_cursor(self.gallery.data_dir, DANBOORU_QUERY)
            history_cursor = cursor_state.get("cursor")
            history_exhausted = cursor_state.get("exhausted") is True
            history_page_by_post_id: dict[int, str] = {}
            capacity_blocked = False
            latest_last_processed_id: int | None = None
            latest_new_count = 0
            latest_fully_scanned = True
            try:
                latest_candidates = await fetch_danbooru_images(
                    self.config.update_count, self.http_client
                )
                request_succeeded = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_request_failure(exc)
                last_error = f"{type(exc).__name__}: {exc}"
                latest_candidates = []

            if latest_candidates:
                for candidate in latest_candidates:
                    latest_last_processed_id = candidate["post_id"]
                    if candidate.get("file_size", 0) > MAX_IMAGE_SIZE:
                        continue
                    file_ext = str(candidate.get("file_ext") or "")
                    extension = Path(file_ext).suffix.lower()
                    if not extension and file_ext:
                        extension = f".{file_ext.lstrip('.').lower()}"
                    if not extension:
                        extension = Path(
                            candidate["file_url"].split("?", 1)[0]
                        ).suffix.lower()
                    if extension == ".jpeg":
                        extension = ".jpg"
                    if extension not in ALLOWED_EXTENSIONS:
                        continue
                    post_id = candidate["post_id"]
                    expected_hash = candidate["md5"]
                    if post_id in pending_post_ids or (
                        expected_hash and expected_hash in pending_hashes
                    ):
                        continue
                    if await self.gallery.database.get_image_by_post_id(post_id):
                        continue
                    if expected_hash and await self.gallery.database.get_image_by_hash(
                        expected_hash
                    ):
                        continue
                    latest_new_count += 1
                    if len(pending) >= self.config.update_count:
                        continue
                    removed = await self.gallery.ensure_capacity(self.config.max_images)
                    if removed:
                        self.logger.info(
                            "Capacity control removed %s old image(s).", removed
                        )
                    if (
                        len(await self.gallery.database.list_images()) + len(pending)
                        >= self.config.max_images
                    ):
                        self.logger.warning(
                            "Gallery capacity is occupied by manual images; skipping new download."
                        )
                        capacity_blocked = True
                        latest_fully_scanned = False
                        break
                    pending.append(candidate)
                    pending_post_ids.add(post_id)
                    if expected_hash:
                        pending_hashes.add(expected_hash)
                if (
                    history_cursor is None
                    and not history_exhausted
                    and latest_new_count <= self.config.update_count
                    and latest_fully_scanned
                ):
                    if latest_last_processed_id is not None:
                        history_cursor = f"b{latest_last_processed_id}"

            while (
                len(pending) < self.config.update_count
                and history_cursor
                and not history_exhausted
                and not capacity_blocked
            ):
                page_start_cursor = history_cursor
                try:
                    candidates = await fetch_danbooru_images(
                        self.config.update_count, self.http_client, page_start_cursor
                    )
                    request_succeeded = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log_request_failure(exc)
                    last_error = f"{type(exc).__name__}: {exc}"
                    break
                if not candidates:
                    history_exhausted = True
                    break
                page_fully_scanned = True
                page_last_processed_id: int | None = None
                for candidate in candidates:
                    if len(pending) >= self.config.update_count:
                        page_fully_scanned = False
                        break
                    page_last_processed_id = candidate["post_id"]
                    if candidate.get("file_size", 0) > MAX_IMAGE_SIZE:
                        continue
                    file_ext = str(candidate.get("file_ext") or "")
                    extension = Path(file_ext).suffix.lower()
                    if not extension and file_ext:
                        extension = f".{file_ext.lstrip('.').lower()}"
                    if not extension:
                        extension = Path(
                            candidate["file_url"].split("?", 1)[0]
                        ).suffix.lower()
                    if extension == ".jpeg":
                        extension = ".jpg"
                    if extension not in ALLOWED_EXTENSIONS:
                        continue
                    post_id = candidate["post_id"]
                    expected_hash = candidate["md5"]
                    if post_id in pending_post_ids or (
                        expected_hash and expected_hash in pending_hashes
                    ):
                        continue
                    if await self.gallery.database.get_image_by_post_id(post_id):
                        continue
                    if expected_hash and await self.gallery.database.get_image_by_hash(
                        expected_hash
                    ):
                        continue
                    removed = await self.gallery.ensure_capacity(self.config.max_images)
                    if removed:
                        self.logger.info(
                            "Capacity control removed %s old image(s).", removed
                        )
                    if (
                        len(await self.gallery.database.list_images()) + len(pending)
                        >= self.config.max_images
                    ):
                        self.logger.warning(
                            "Gallery capacity is occupied by manual images; skipping new download."
                        )
                        capacity_blocked = True
                        break
                    pending.append(candidate)
                    pending_post_ids.add(post_id)
                    history_page_by_post_id[post_id] = page_start_cursor
                    if expected_hash:
                        pending_hashes.add(expected_hash)
                if capacity_blocked:
                    break
                if not page_fully_scanned:
                    if page_last_processed_id is not None:
                        history_cursor = f"b{page_last_processed_id}"
                    break
                next_cursor = f"b{min(item['post_id'] for item in candidates)}"
                if next_cursor == page_start_cursor:
                    history_exhausted = True
                    break
                history_cursor = next_cursor
            download_limit = asyncio.Semaphore(self.config.download_concurrency)

            async def download_candidate(
                candidate: dict[str, Any],
            ) -> tuple[dict[str, Any], Path | None, Exception | None]:
                path: Path | None = None
                try:
                    async with download_limit:
                        path = await self.downloader.download(
                            candidate["file_url"],
                            candidate["post_id"],
                            self.gallery.images_dir,
                            candidate["file_ext"],
                        )
                    local_hash = await asyncio.to_thread(
                        lambda: hashlib.md5(path.read_bytes()).hexdigest()
                    )
                    expected_hash = candidate["md5"]
                    if expected_hash and local_hash.lower() != expected_hash.lower():
                        raise ValueError(
                            "downloaded file hash does not match Danbooru md5"
                        )
                    return candidate, path, None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if path is not None:
                        path.unlink(missing_ok=True)
                    return candidate, None, exc

            downloaded = await asyncio.gather(
                *(download_candidate(candidate) for candidate in pending)
            )
            history_retry_cursor: str | None = None
            for candidate, path, error in downloaded:
                post_id = candidate["post_id"]
                if error is not None:
                    history_retry_cursor = (
                        history_retry_cursor or history_page_by_post_id.get(post_id)
                    )
                    self.logger.warning(
                        "Image download failed for post %s: %s", post_id, error
                    )
                    continue
                assert path is not None
                try:
                    image_id = await self.gallery.database.add_image(
                        {
                            "post_id": post_id,
                            "file_name": path.name,
                            "file_path": str(path.resolve()),
                            "file_hash": candidate["md5"]
                            or hashlib.md5(path.read_bytes()).hexdigest(),
                            "source_url": candidate["file_url"],
                            "created_at": candidate["created_at"],
                            "downloaded_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                        },
                        candidate["tags"],
                    )
                    if image_id is None:
                        path.unlink(missing_ok=True)
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    history_retry_cursor = (
                        history_retry_cursor or history_page_by_post_id.get(post_id)
                    )
                    path.unlink(missing_ok=True)
                    self.logger.exception("Image indexing failed for post %s.", post_id)
                    continue
                added_total += 1
                tags = set(candidate["tags"])
                for target_tag in added_by_tag:
                    if target_tag in tags:
                        added_by_tag[target_tag] += 1
            if request_succeeded:
                if history_retry_cursor:
                    history_cursor = history_retry_cursor
                    history_exhausted = False
                save_danbooru_cursor(
                    self.gallery.data_dir,
                    history_cursor,
                    history_exhausted,
                    DANBOORU_QUERY,
                )
                self.gallery.save_state(added_total, added_by_tag)
            else:
                self.gallery.save_failure(last_error or "Danbooru request failed")
            self.logger.info(
                "Gallery update for %s completed; %s image(s) added.",
                CORE_TAG,
                added_total,
            )

    @staticmethod
    def is_to_me(event: AstrMessageEvent) -> bool:
        # 私聊直接算 to_me
        if event.get_group_id() is None:
            return True
        # 群聊必须 @Bot / 被唤醒
        return event.is_at_or_wake_command

    # @filter.command("洁图", alias={"来张洁图"})
    @filter.regex(r"^(?!.*洁图统计)(?=.*洁图|(?=.*安洁)(?=.*照片)).*$")
    async def random_image(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """抽选一只小可爱"""
        if not self.is_to_me(event):
            return
        image = await self.gallery.random_image(CORE_TAG)
        if image is None:
            yield event.plain_result("图库当前没有可用的洁图。")
            return

        yield event.chain_result(
            [
                Comp.At(qq=event.get_sender_id()),
                Comp.Plain(" 博士，您的小可爱已抵达！\n"),
                Comp.Image.fromFileSystem(path=str(Path(image["file_path"]).resolve())),
                Comp.Plain(f"\n嘿嘿，拍的怎么样啊博士？我好看吗？"),
            ]
        )
        return

    @filter.command("洁图统计")
    async def image_statistics(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """基于Pillow生成的统计图片"""
        try:
            output = await generate_statistics_image(self.gallery)
        except Exception:
            self.logger.exception("Gallery statistics image generation failed.")
            yield event.plain_result("洁图统计生成失败，请检查图库图片和中文字体。")
            return
        if output is None:
            yield event.plain_result("图库当前没有可用于生成统计图的图片。")
            return
        yield event.image_result(str(output.resolve()))
