from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite


class GalleryDatabase:
    """Asynchronous SQLite repository for gallery images and their tags."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Create the database schema and indexes.

        Raises:
            aiosqlite.Error: If SQLite cannot open or initialize the database.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER UNIQUE,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                file_hash TEXT,
                source_url TEXT,
                created_at TEXT,
                downloaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS image_tags (
                image_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (image_id, tag_id),
                FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_images_post_id ON images(post_id);
            CREATE INDEX IF NOT EXISTS idx_images_file_hash ON images(file_hash);
            CREATE INDEX IF NOT EXISTS idx_image_tags_image_id ON image_tags(image_id);
            CREATE INDEX IF NOT EXISTS idx_image_tags_tag_id ON image_tags(tag_id);
            """
        )
        await self.connection.commit()

    async def close(self) -> None:
        """Close the database connection if it is open."""
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def _require_connection(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("gallery database is not initialized")
        return self.connection

    async def get_image_by_post_id(self, post_id: int) -> dict[str, Any] | None:
        """Return an image by its unique Danbooru post ID."""
        db = self._require_connection()
        cursor = await db.execute("SELECT * FROM images WHERE post_id = ?", (post_id,))
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def get_image_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        """Return an image by its local or Danbooru hash."""
        db = self._require_connection()
        cursor = await db.execute(
            "SELECT * FROM images WHERE file_hash = ?", (file_hash,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def get_image_by_path(self, file_path: str) -> dict[str, Any] | None:
        """Return an image by its normalized local path."""
        db = self._require_connection()
        cursor = await db.execute(
            "SELECT * FROM images WHERE file_path = ?", (file_path,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def add_image(self, image: dict[str, Any], tags: list[str]) -> int | None:
        """Insert an image and all tag relationships in one transaction.

        Args:
            image: Image metadata. It must contain file_name, file_path and
                downloaded_at; other fields may be null.
            tags: Complete canonical Danbooru tag list.

        Returns:
            The inserted image ID, or None when the post ID or path already exists.
        """
        db = self._require_connection()
        try:
            async with db.execute(
                """
                INSERT INTO images
                (post_id, file_name, file_path, file_hash, source_url, created_at, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image.get("post_id"),
                    image["file_name"],
                    image["file_path"],
                    image.get("file_hash"),
                    image.get("source_url"),
                    image.get("created_at"),
                    image["downloaded_at"],
                ),
            ) as cursor:
                image_id = cursor.lastrowid
            for tag in dict.fromkeys(tag for tag in tags if tag):
                await db.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
                cursor = await db.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                row = await cursor.fetchone()
                await cursor.close()
                if row:
                    await db.execute(
                        "INSERT OR IGNORE INTO image_tags(image_id, tag_id) VALUES (?, ?)",
                        (image_id, row[0]),
                    )
            await db.commit()
            return int(image_id)
        except aiosqlite.IntegrityError:
            await db.rollback()
            return None
        except Exception:
            await db.rollback()
            raise

    async def add_tags(self, image_id: int, tags: list[str]) -> None:
        """Associate additional canonical tags with an existing image."""
        db = self._require_connection()
        try:
            for tag in dict.fromkeys(tag for tag in tags if tag):
                await db.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
                cursor = await db.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                row = await cursor.fetchone()
                await cursor.close()
                if row:
                    await db.execute(
                        "INSERT OR IGNORE INTO image_tags(image_id, tag_id) VALUES (?, ?)",
                        (image_id, row[0]),
                    )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def remove_image(self, image_id: int) -> None:
        """Delete an image and clean tags no longer referenced by any image."""
        db = self._require_connection()
        await db.execute("DELETE FROM images WHERE id = ?", (image_id,))
        await db.execute(
            "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM image_tags)"
        )
        await db.commit()

    async def remove_missing_files(self) -> int:
        """Remove database records whose local files no longer exist."""
        db = self._require_connection()
        cursor = await db.execute("SELECT id, file_path FROM images")
        rows = await cursor.fetchall()
        await cursor.close()
        removed = 0
        for row in rows:
            if not Path(row["file_path"]).is_file():
                await db.execute("DELETE FROM images WHERE id = ?", (row["id"],))
                removed += 1
        if removed:
            await db.execute(
                "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM image_tags)"
            )
            await db.commit()
        return removed

    async def list_images(self) -> list[dict[str, Any]]:
        """Return all image records."""
        db = self._require_connection()
        cursor = await db.execute("SELECT * FROM images ORDER BY id")
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def random_image(self, tag: str | None = None) -> dict[str, Any] | None:
        """Return one random image, optionally restricted to a tag."""
        db = self._require_connection()
        if tag:
            query = """
                SELECT DISTINCT images.* FROM images
                JOIN image_tags ON images.id = image_tags.image_id
                JOIN tags ON tags.id = image_tags.tag_id
                WHERE tags.name = ? ORDER BY RANDOM() LIMIT 1
            """
            params: tuple[Any, ...] = (tag,)
        else:
            query = "SELECT * FROM images ORDER BY RANDOM() LIMIT 1"
            params = ()
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def count_for_tag(self, tag: str) -> int:
        """Count distinct images associated with a tag."""
        db = self._require_connection()
        cursor = await db.execute(
            """
            SELECT COUNT(DISTINCT image_tags.image_id) AS count
            FROM image_tags JOIN tags ON tags.id = image_tags.tag_id
            WHERE tags.name = ?
            """,
            (tag,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["count"] if row else 0)

    async def count_distinct_for_tags(self, tags: list[str]) -> int:
        """Count distinct images associated with at least one of the tags.

        Args:
            tags: Canonical Danbooru tag names.

        Returns:
            Number of unique images matching any tag.
        """
        unique_tags = list(dict.fromkeys(tags))
        if not unique_tags:
            return 0
        db = self._require_connection()
        placeholders = ", ".join("?" for _ in unique_tags)
        cursor = await db.execute(
            f"""
            SELECT COUNT(DISTINCT image_tags.image_id) AS count
            FROM image_tags JOIN tags ON tags.id = image_tags.tag_id
            WHERE tags.name IN ({placeholders})
            """,
            unique_tags,
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["count"] if row else 0)

    async def random_image_for_tag(self, tag: str) -> dict[str, Any] | None:
        """Return one random image associated with a tag."""
        return await self.random_image(tag)

    async def oldest_automatic(self) -> dict[str, Any] | None:
        """Return the oldest automatically downloaded image."""
        db = self._require_connection()
        cursor = await db.execute(
            "SELECT * FROM images WHERE post_id IS NOT NULL ORDER BY downloaded_at ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def tags_for_image(self, image_id: int) -> set[str]:
        """Return all canonical tags associated with an image."""
        db = self._require_connection()
        cursor = await db.execute(
            """
            SELECT tags.name FROM tags JOIN image_tags ON tags.id = image_tags.tag_id
            WHERE image_tags.image_id = ?
            """,
            (image_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row[0]) for row in rows}
