from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

danbooru = importlib.import_module("astrbot_plugin_gallery.api.danbooru")
config_module = importlib.import_module("astrbot_plugin_gallery.config")
gallery_module = importlib.import_module("astrbot_plugin_gallery.gallery")
main_module = importlib.import_module("astrbot_plugin_gallery.main")
statistics_module = importlib.import_module("astrbot_plugin_gallery.statistics")

Gallery = gallery_module.Gallery
TAG_MAPPING = gallery_module.TAG_MAPPING


def test_download_concurrency_config_has_safe_default() -> None:
    """Use two concurrent downloads by default and reject invalid values."""

    assert config_module.GalleryConfig.from_dict({}).download_concurrency == 2
    assert (
        config_module.GalleryConfig.from_dict(
            {"download_concurrency": 4}
        ).download_concurrency
        == 4
    )
    assert (
        config_module.GalleryConfig.from_dict(
            {"download_concurrency": 0}
        ).download_concurrency
        == 2
    )


def test_proxy_config_is_optional_and_normalized() -> None:
    """Use no proxy by default and trim configured proxy URLs."""

    assert config_module.GalleryConfig.from_dict({}).proxy == ""
    assert (
        config_module.GalleryConfig.from_dict(
            {"proxy": "  http://127.0.0.1:7890  "}
        ).proxy
        == "http://127.0.0.1:7890"
    )
    assert config_module.GalleryConfig.from_dict({"proxy": 123}).proxy == ""


def test_cookie_config_is_parsed_for_cloudflare() -> None:
    """Turn a pasted browser cookie string into a request mapping."""
    assert config_module.GalleryConfig.from_dict({}).cookie == ""
    assert config_module.parse_cookie_header("cf_clearance=abc; __cf_bm=x=y") == {
        "cf_clearance": "abc",
        "__cf_bm": "x=y",
    }
    assert config_module.parse_cookie_header("") == {}


def test_danbooru_auth_uses_basic_header() -> None:
    """Send credentials in a header instead of the query string."""
    header = danbooru._auth_header("KaMmySuma", "secret")
    assert header["authorization"].startswith("Basic ")
    decoded = base64.b64decode(header["authorization"].split(" ", 1)[1]).decode()
    assert decoded == "KaMmySuma:secret"
    assert danbooru._auth_header("", "secret") == {}
    assert danbooru._auth_header("KaMmySuma", "") == {}


def test_danbooru_headers_do_not_override_user_agent() -> None:
    """Keep curl_cffi impersonation in charge of the user agent."""
    assert "user-agent" not in danbooru.DANBOORU_HEADERS
    assert "user-agent" not in danbooru.DANBOORU_IMAGE_HEADERS


def test_database_sync_and_multitag(tmp_path: Path) -> None:
    """Synchronize manual files and preserve many tags for automatic files."""

    async def scenario() -> None:
        gallery = Gallery(tmp_path)
        await gallery.initialize()
        manual = gallery.images_dir / "manual.jpg"
        Image.new("RGB", (100, 100), (255, 0, 0)).save(manual)
        await gallery.sync_local_images()
        assert len(await gallery.database.list_images()) == 1

        automatic = gallery.images_dir / "11919554.jpg"
        Image.new("RGB", (100, 100), (0, 255, 0)).save(automatic)
        await gallery.database.add_image(
            {
                "post_id": 11919554,
                "file_name": automatic.name,
                "file_path": str(automatic.resolve()),
                "file_hash": "hash",
                "source_url": "https://cdn.example/11919554.jpg",
                "created_at": "2026-08-22T00:00:00Z",
                "downloaded_at": "2026-08-22T00:00:00",
            },
            [TAG_MAPPING["安洁莉娜"], TAG_MAPPING["素质访客"]],
        )
        assert await gallery.database.count_for_tag(TAG_MAPPING["安洁莉娜"]) == 1
        assert await gallery.database.count_for_tag(TAG_MAPPING["素质访客"]) == 1
        await gallery.close()

    asyncio.run(scenario())


def test_sector_focus_uses_two_thirds_of_the_radius() -> None:
    """Place the visual focus on the sector axis at the fixed radial ratio."""
    center = (330.0, 425.0)
    radius = 260.0
    actual = statistics_module._calculate_sector_focus(center, radius, -90.0, 90.0)
    middle_angle = math.radians(-45.0)
    expected = (
        center[0] + radius * (2 / 3) * math.cos(middle_angle),
        center[1] + radius * (2 / 3) * math.sin(middle_angle),
    )
    assert actual == pytest.approx(expected)


@pytest.mark.parametrize(
    ("start", "extent", "expected"),
    [
        (-90.0, 90.0, (330.0, 165.0, 590.0, 425.0)),
        (-30.0, 240.0, (70.0, 295.0, 590.0, 685.0)),
        (
            350.0,
            30.0,
            (330.0, 379.8514738065981, 590.0, 513.9252372646739),
        ),
        (-90.0, 360.0, (70.0, 165.0, 590.0, 685.0)),
    ],
)
def test_sector_bounds_are_exact(
    start: float,
    extent: float,
    expected: tuple[float, float, float, float],
) -> None:
    """Include endpoints and every cardinal extremum covered by the sector."""
    actual = statistics_module._calculate_sector_bounds(
        (330.0, 425.0), 260.0, start, extent
    )
    assert actual == pytest.approx(expected)


@pytest.mark.parametrize(
    ("source_size", "anchor", "start", "extent"),
    [
        ((800, 600), (143.0, 79.0), -90.0, 90.0),
        ((320, 900), (20.0, 450.0), 20.0, 270.0),
        ((1200, 120), (2.0, 1.0), 350.0, 5.0),
        ((1000, 400), (999.0, 399.0), 160.0, 20.0),
    ],
)
def test_focus_transform_aligns_anchor_and_covers_sector_bounds(
    source_size: tuple[int, int],
    anchor: tuple[float, float],
    start: float,
    extent: float,
) -> None:
    """Use one exact scale for narrow sectors, extreme ratios, and edge anchors."""
    center = (330.0, 425.0)
    radius = 260.0
    output_size = (520, 520)
    visual_focus = statistics_module._calculate_sector_focus(
        center, radius, start, extent
    )
    sector_bounds = statistics_module._calculate_sector_bounds(
        center, radius, start, extent
    )
    scale, image_origin = statistics_module._calculate_focus_transform(
        source_size, output_size, anchor, visual_focus, sector_bounds
    )

    final_anchor = (
        image_origin[0] + anchor[0] * scale,
        image_origin[1] + anchor[1] * scale,
    )
    image_bounds = (
        image_origin[0],
        image_origin[1],
        image_origin[0] + source_size[0] * scale,
        image_origin[1] + source_size[1] * scale,
    )
    assert final_anchor == pytest.approx(visual_focus)
    assert image_bounds[0] <= sector_bounds[0] + 1e-9
    assert image_bounds[1] <= sector_bounds[1] + 1e-9
    assert image_bounds[2] >= sector_bounds[2] - 1e-9
    assert image_bounds[3] >= sector_bounds[3] - 1e-9


def test_invalid_focus_falls_back_to_normal_cover() -> None:
    """Use the normal centered cover for malformed or non-finite anchors."""
    image = Image.new("RGB", (800, 600))
    expected = statistics_module._cover(image, (520, 520)).tobytes()
    invalid_focuses = (None, ("abc", 123), (float("nan"), 10), (float("inf"), 20))
    for focus in invalid_focuses:
        result = statistics_module._cover_with_focus(
            image,
            (520, 520),
            focus,
            canvas_center=(330.0, 425.0),
            canvas_radius=260.0,
            start=-90.0,
            extent=90.0,
        )
        assert result.tobytes() == expected


def test_danbooru_adapter_normalizes_complete_payload() -> None:
    """Use the fixed Safe core-tag request and preserve complete tags."""

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["tags"] == "angelina_(arknights) rating:g,s"
            assert request.url.params["search[order]"] == "id_desc"
            assert request.url.params["page"] == "b41"
            assert request.url.params["limit"] == "200"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "created_at": "2026-08-22T00:00:00Z",
                        "md5": "abc",
                        "file_ext": "jpg",
                        "tag_string": "1girl angelina_(arknights) fox_ears",
                        "file_url": "https://cdn.example/42.jpg",
                        "file_size": 1024,
                    }
                ],
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await danbooru.fetch_danbooru_images(20, client, "b41")
        assert result[0]["post_id"] == 42
        assert result[0]["tags"] == ["1girl", "angelina_(arknights)", "fox_ears"]
        assert result[0]["file_size"] == 1024

    asyncio.run(scenario())


def test_danbooru_cursor_round_trip(tmp_path: Path) -> None:
    """Persist and reload the historical cursor and exhausted state."""
    query = danbooru.DANBOORU_QUERY
    danbooru.save_danbooru_cursor(tmp_path, "b123", False, query)
    assert danbooru.load_danbooru_cursor(tmp_path, query) == {
        "cursor": "b123",
        "exhausted": False,
    }

    danbooru.save_danbooru_cursor(tmp_path, "b1", True, query)
    assert danbooru.load_danbooru_cursor(tmp_path, query) == {
        "cursor": "b1",
        "exhausted": True,
    }


def test_danbooru_cursor_resets_when_query_changes(tmp_path: Path) -> None:
    """Discard a cursor written for a different tag query so history is rewalked."""
    danbooru.save_danbooru_cursor(tmp_path, "b1", True, "angelina_(arknights) rating:s")
    assert danbooru.load_danbooru_cursor(tmp_path, danbooru.DANBOORU_QUERY) == {}


def test_update_skips_unsupported_formats_without_blocking_count_or_cursor(
    tmp_path: Path, monkeypatch
) -> None:
    """Fill the update target and advance history past unsupported posts."""

    async def scenario() -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.images: list[dict] = []

            async def get_image_by_post_id(self, post_id: int) -> None:
                return None

            async def get_image_by_hash(self, file_hash: str) -> None:
                return None

            async def list_images(self) -> list[dict]:
                return self.images

            async def add_image(self, image: dict, tags: list[str]) -> int:
                self.images.append(image)
                return len(self.images)

        class FakeGallery:
            def __init__(self) -> None:
                self.data_dir = tmp_path
                self.images_dir = tmp_path / "images"
                self.images_dir.mkdir()
                self.database = FakeDatabase()
                self.saved_total = 0

            async def sync_local_images(self) -> None:
                return None

            async def ensure_capacity(self, max_images: int) -> int:
                return 0

            def save_state(self, added_total: int, added_by_tag: dict) -> None:
                self.saved_total = added_total

        class FakeDownloader:
            def __init__(self) -> None:
                self.post_ids: list[int] = []

            async def download(
                self,
                url: str,
                post_id: int,
                target_dir: Path,
                file_ext: str = "",
            ) -> Path:
                self.post_ids.append(post_id)
                path = target_dir / f"{post_id}.jpg"
                path.write_bytes(b"image")
                return path

        requested_pages: list[str | None] = []

        async def fake_fetch(count: int, client, page=None) -> list[dict]:
            requested_pages.append(page)
            if page is None:
                return [
                    {
                        "post_id": post_id,
                        "created_at": "2026-08-23T00:00:00Z",
                        "file_url": f"https://cdn.example/{post_id}.webm",
                        "file_ext": "webm",
                        "file_size": 1024,
                        "md5": "",
                        "tags": [TAG_MAPPING["安洁莉娜"]],
                    }
                    for post_id in range(400, 200, -1)
                ]
            assert page == "b201"
            return [
                {
                    "post_id": post_id,
                    "created_at": "2026-08-23T00:00:00Z",
                    "file_url": f"https://cdn.example/{post_id}.{file_ext}",
                    "file_ext": file_ext,
                    "file_size": 1024,
                    "md5": "",
                    "tags": [TAG_MAPPING["安洁莉娜"]],
                }
                for post_id, file_ext in (
                    (200, "mp4"),
                    (199, "zip"),
                    (198, "jpg"),
                    (197, "png"),
                    (196, "jpg"),
                )
            ]

        monkeypatch.setattr(main_module, "fetch_danbooru_images", fake_fetch)
        plugin = main_module.GalleryPlugin.__new__(main_module.GalleryPlugin)
        plugin._update_lock = asyncio.Lock()
        plugin.http_client = object()
        plugin.downloader = FakeDownloader()
        plugin.gallery = FakeGallery()
        plugin.config = SimpleNamespace(
            update_count=2, max_images=100, download_concurrency=2
        )
        plugin.logger = logging.getLogger(__name__)

        await plugin.update_gallery()

        assert requested_pages == [None, "b201"]
        assert plugin.downloader.post_ids == [198, 197]
        assert plugin.gallery.saved_total == 2
        assert danbooru.load_danbooru_cursor(tmp_path) == {
            "cursor": "b197",
            "exhausted": False,
        }

    asyncio.run(scenario())
