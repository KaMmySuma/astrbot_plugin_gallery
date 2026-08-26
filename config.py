from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GalleryConfig:
    """Validated runtime configuration for the gallery."""

    auto_update_enabled: bool = True
    update_interval_minutes: int = 60
    update_count: int = 20
    max_images: int = 10000
    download_concurrency: int = 2
    proxy: str = ""

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
        )
