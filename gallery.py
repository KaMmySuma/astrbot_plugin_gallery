from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .database import GalleryDatabase

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TAG_MAPPING = {
    "安洁莉娜": "angelina_(arknights)",
    "洁尔佩塔": "gilberta_(arknights)",
    "予愿安洁莉娜": "angelina_the_mellow_wish_(arknights)",
    "夏卉": "angelina_(summer_flower)_(arknights)",
    "素质访客": "angelina_(distinguished_visitor)_(arknights)",
    "健身时刻": "angelina_(sports)_(arknights)",
    "前航远歌": "angelina_(ambience_synesthesia_2021)_(arknights)",
    "冰纹玉釉": "angelina_(ambience_synesthesia_2025)_(arknights)",
}
CHARACTER_CONFIG = {
    "angelina_(arknights)": {"display_name": "安洁莉娜", "statistics_tags": TAG_MAPPING}
}
POST_ID_PATTERN = re.compile(r"^(?:danbooru_)?(\d+)$", re.IGNORECASE)


class Gallery:
    """Business logic for a unified image directory and SQLite metadata."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.images_dir = data_dir / "images"
        self.output_dir = Path(get_astrbot_temp_path()) / "astrbot_plugin_gallery"
        self.state_path = data_dir / "update_state.json"
        self.database = GalleryDatabase(data_dir / "gallery.db")

    async def initialize(self) -> None:
        """Create directories, initialize SQLite, and synchronize local files."""
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        await self.database.initialize()
        await self._migrate_legacy_directories()
        legacy_generated = self.data_dir / "generated"
        if legacy_generated.is_dir():
            for generated_file in legacy_generated.iterdir():
                if generated_file.is_file():
                    generated_file.unlink(missing_ok=True)
            try:
                legacy_generated.rmdir()
            except OSError:
                pass
        await self.sync_local_images()

    async def close(self) -> None:
        """Close the gallery database."""
        await self.database.close()

    async def sync_local_images(self) -> None:
        """Synchronize database records with supported files in images/.

        Files in legacy subdirectories are intentionally ignored. New manual
        files receive no tags because their Danbooru metadata is unavailable.
        """
        await self.database.remove_missing_files()
        known = {
            str(Path(row["file_path"]).resolve())
            for row in await self.database.list_images()
        }
        for path in self.images_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            resolved = str(path.resolve())
            if resolved in known:
                continue
            digest = await asyncio.to_thread(
                lambda image_path=path: hashlib.md5(image_path.read_bytes()).hexdigest()
            )
            existing = await self.database.get_image_by_hash(digest)
            if existing:
                continue
            match = POST_ID_PATTERN.fullmatch(path.stem)
            post_id = int(match.group(1)) if match else None
            await self.database.add_image(
                {
                    "post_id": post_id,
                    "file_name": path.name,
                    "file_path": resolved,
                    "file_hash": digest,
                    "source_url": None,
                    "created_at": None,
                    "downloaded_at": datetime.now().isoformat(timespec="seconds"),
                },
                [],
            )

    async def _migrate_legacy_directories(self) -> None:
        """Consolidate the previous eight-folder layout without losing known tags."""
        for display_name, english_tag in TAG_MAPPING.items():
            legacy_dir = self.images_dir / display_name
            if not legacy_dir.is_dir():
                continue
            for source in legacy_dir.iterdir():
                if (
                    not source.is_file()
                    or source.suffix.lower() not in SUPPORTED_EXTENSIONS
                ):
                    continue
                digest = await asyncio.to_thread(
                    lambda image_path=source: hashlib.md5(
                        image_path.read_bytes()
                    ).hexdigest()
                )
                existing = await self.database.get_image_by_hash(digest)
                if existing:
                    await self.database.add_tags(existing["id"], [english_tag])
                    source.unlink(missing_ok=True)
                    continue
                match = POST_ID_PATTERN.fullmatch(source.stem)
                post_id = int(match.group(1)) if match else None
                if post_id is not None:
                    existing = await self.database.get_image_by_post_id(post_id)
                    if existing:
                        await self.database.add_tags(existing["id"], [english_tag])
                        source.unlink(missing_ok=True)
                        continue
                name = f"{post_id}{source.suffix.lower()}" if post_id else source.name
                destination = self.images_dir / name
                if destination.exists():
                    name = f"{destination.stem}_{digest[:8]}{destination.suffix}"
                    destination = self.images_dir / name
                source.replace(destination)
                image_id = await self.database.add_image(
                    {
                        "post_id": post_id,
                        "file_name": destination.name,
                        "file_path": str(destination.resolve()),
                        "file_hash": digest,
                        "source_url": None,
                        "created_at": None,
                        "downloaded_at": datetime.fromtimestamp(
                            destination.stat().st_mtime
                        ).isoformat(timespec="seconds"),
                    },
                    [english_tag],
                )
                if image_id is None:
                    destination.unlink(missing_ok=True)
            try:
                legacy_dir.rmdir()
            except OSError:
                pass

    async def ensure_capacity(self, max_images: int) -> int:
        """Evict oldest automatic files until the configured limit is met."""
        removed = 0
        while len(await self.database.list_images()) >= max_images:
            image = await self.database.oldest_automatic()
            if image is None:
                break
            Path(image["file_path"]).unlink(missing_ok=True)
            await self.database.remove_image(image["id"])
            removed += 1
        return removed

    def load_state(self) -> dict[str, Any]:
        """Load the latest update status without treating it as image data."""
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save_state(self, added_total: int, added_by_tag: dict[str, int]) -> None:
        """Persist the latest successful update summary atomically."""
        payload = {
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_added_total": added_total,
            "last_added_by_tag": added_by_tag,
        }
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)

    async def random_image(self, tag: str | None = None) -> dict[str, Any] | None:
        """Return a random image record, removing stale records on demand."""
        for _ in range(3):
            image = await self.database.random_image(tag)
            if image is None:
                return None
            if Path(image["file_path"]).is_file():
                return image
            await self.database.remove_image(image["id"])
        return None
