from __future__ import annotations

import logging
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    UnidentifiedImageError,
)

from .gallery import TAG_MAPPING, Gallery

logger = logging.getLogger(__name__)

TAG_COLORS = {
    "安洁莉娜": (236, 100, 105),
    "洁尔佩塔": (111, 190, 188),
    "予愿安洁莉娜": (245, 186, 70),
    "夏卉": (104, 155, 230),
    "素质访客": (145, 112, 218),
    "健身时刻": (126, 193, 103),
    "前航远歌": (234, 137, 180),
    "冰纹玉釉": (115, 171, 208),
}
RESOURCE_DIR = Path(__file__).parent / "resource"
TAG_FONT_PATH = RESOURCE_DIR / "front" / "Maple UI.ttf"
META_FONT_PATH = RESOURCE_DIR / "front" / "熊兔流星萌系体.ttf"
TITLE_IMAGE_PATH = RESOURCE_DIR / "pic" / "安洁莉娜.png"


class TagCoverConfig(TypedDict, total=False):
    """Configuration for an optional manually selected Tag cover."""

    path: Path | str
    face: tuple[float, float]
    face_radius: float


TAG_COVERS: dict[str, TagCoverConfig] = {
    "安洁莉娜": {"path": RESOURCE_DIR / "tag_pic" / "安洁莉娜.png", "face": (519, 232)},
    "洁尔佩塔": {"path": RESOURCE_DIR / "tag_pic" / "洁尔佩塔.png", "face": (717, 582)},
    "予愿安洁莉娜": {
        "path": RESOURCE_DIR / "tag_pic" / "予愿安洁莉娜.png",
        "face": (376, 426),
    },
    "夏卉": {"path": RESOURCE_DIR / "tag_pic" / "夏卉.png", "face": (413, 233)},
    "素质访客": {"path": RESOURCE_DIR / "tag_pic" / "素质访客.png", "face": (507, 369)},
}


def _font(
    size: int, bold: bool = False, font_path: Path | None = None
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a requested font, falling back to a portable Chinese-capable font.

    Args:
        size: Font size in pixels.
        bold: Whether to prefer a bold system fallback.
        font_path: Plugin font path to load before checking system fonts.

    Returns:
        A Pillow font instance.
    """
    if font_path and font_path.is_file():
        return ImageFont.truetype(str(font_path), size)
    names = (
        ["msyhbd.ttc", "msyh.ttc", "Microsoft YaHei.ttf", "NotoSansCJK-Regular.ttc"]
        if bold
        else ["msyh.ttc", "Microsoft YaHei.ttf", "NotoSansCJK-Regular.ttc"]
    )
    roots = [
        Path("C:/Windows/Fonts"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/System/Library/Fonts"),
    ]
    for root in roots:
        for name in names:
            path = root / name
            if path.is_file():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Crop an image to completely cover a target rectangle."""
    image = image.convert("RGB")
    ratio = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize(
        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))


def _calculate_sector_focus(
    center: tuple[float, float],
    radius: float,
    start: float,
    extent: float,
) -> tuple[float, float]:
    """Calculate the visual focus point on a sector's symmetry axis.

    Args:
        center: Center of the pie chart.
        radius: Radius of the pie chart.
        start: Pillow-compatible start angle of the sector.
        extent: Pillow-compatible angular extent of the sector.

    Returns:
        The point two thirds of the radius from the center along the sector axis.
    """
    middle_angle = math.radians(start + extent / 2)
    focus_radius = radius * (2 / 3)
    return (
        center[0] + focus_radius * math.cos(middle_angle),
        center[1] + focus_radius * math.sin(middle_angle),
    )


def _calculate_sector_bounds(
    center: tuple[float, float],
    radius: float,
    start: float,
    extent: float,
) -> tuple[float, float, float, float]:
    """Calculate the exact axis-aligned bounding box of a circular sector.

    Args:
        center: Center of the pie chart.
        radius: Radius of the pie chart.
        start: Pillow-compatible start angle of the sector.
        extent: Pillow-compatible angular extent of the sector.

    Returns:
        The minimum ``(left, top, right, bottom)`` rectangle containing the sector.
    """
    if radius <= 0 or extent <= 0:
        return (center[0], center[1], center[0], center[1])

    points = [center]
    for angle in (start, start + extent):
        radians = math.radians(angle)
        points.append(
            (
                center[0] + radius * math.cos(radians),
                center[1] + radius * math.sin(radians),
            )
        )

    for cardinal_angle in (0.0, 90.0, 180.0, 270.0):
        if extent >= 360 or (cardinal_angle - start) % 360 <= extent + 1e-9:
            radians = math.radians(cardinal_angle)
            points.append(
                (
                    center[0] + radius * math.cos(radians),
                    center[1] + radius * math.sin(radians),
                )
            )

    x_values, y_values = zip(*points)
    return (min(x_values), min(y_values), max(x_values), max(y_values))


def _calculate_focus_transform(
    source_size: tuple[int, int],
    output_size: tuple[int, int],
    anchor: tuple[float, float],
    visual_focus: tuple[float, float],
    sector_bounds: tuple[float, float, float, float],
) -> tuple[float, tuple[float, float]]:
    """Calculate the minimum scale and origin for a fixed image anchor.

    Args:
        source_size: Width and height of the source image.
        output_size: Width and height of the final crop.
        anchor: Anchor coordinates in the source image.
        visual_focus: Target canvas coordinate for the anchor.
        sector_bounds: Axis-aligned bounds that the transformed image must cover.

    Returns:
        The minimum uniform scale and transformed image origin on the canvas.

    Raises:
        ValueError: The anchor lies on an edge that must extend past the focus.
    """
    source_width, source_height = source_size
    focus_x, focus_y = anchor
    bbox_left, bbox_top, bbox_right, bbox_bottom = sector_bounds
    left_distance = visual_focus[0] - bbox_left
    top_distance = visual_focus[1] - bbox_top

    # An anchor on the source's left or top edge cannot create image area beyond it.
    if (focus_x == 0 and left_distance > 1e-9) or (
        focus_y == 0 and top_distance > 1e-9
    ):
        raise ValueError(
            "the fixed anchor cannot cover the complete sector bounding box"
        )

    scale = max(
        output_size[0] / source_width,
        output_size[1] / source_height,
        left_distance / focus_x if focus_x > 0 else 0.0,
        top_distance / focus_y if focus_y > 0 else 0.0,
        (bbox_right - visual_focus[0]) / (source_width - focus_x),
        (bbox_bottom - visual_focus[1]) / (source_height - focus_y),
    )
    return scale, (
        visual_focus[0] - focus_x * scale,
        visual_focus[1] - focus_y * scale,
    )


def _cover_with_focus(
    image: Image.Image,
    size: tuple[int, int],
    focus: tuple[float, float],
    *,
    canvas_center: tuple[float, float],
    canvas_radius: float,
    start: float,
    extent: float,
    face_radius: float = 40.0,
    tag: str = "<unknown>",
) -> Image.Image:
    """Cover a square while fixing a source anchor at the sector visual focus.

    Args:
        image: Source image whose focus coordinates are in original pixels.
        size: Output crop size in pixels.
        focus: Original-image coordinate to keep visible.
        canvas_center: Center of the pie chart on the final canvas.
        canvas_radius: Pie chart radius on the final canvas.
        start: Pillow-compatible start angle of the current sector.
        extent: Pillow-compatible angular extent of the current sector.
        face_radius: Retained for Tag configuration compatibility; unused in layout.
        tag: Tag name used in diagnostic logs.

    Returns:
        A transformed image whose anchor matches the sector visual focus.

    Raises:
        ValueError: The fixed anchor makes complete sector coverage impossible.
    """
    image = image.convert("RGB")
    source_width, source_height = image.size
    try:
        focus_x = float(focus[0])
        focus_y = float(focus[1])
        if not math.isfinite(focus_x) or not math.isfinite(focus_y):
            raise ValueError("focus must be finite")
        if not (0 <= focus_x < source_width and 0 <= focus_y < source_height):
            raise ValueError("focus must be inside source image")
    except (TypeError, ValueError, IndexError, OverflowError):
        return _cover(image, size)
    visual_focus = _calculate_sector_focus(canvas_center, canvas_radius, start, extent)
    sector_bounds = _calculate_sector_bounds(
        canvas_center, canvas_radius, start, extent
    )
    required_scale, image_origin = _calculate_focus_transform(
        image.size,
        size,
        (focus_x, focus_y),
        visual_focus,
        sector_bounds,
    )
    output_origin = (
        canvas_center[0] - canvas_radius,
        canvas_center[1] - canvas_radius,
    )
    logger.debug(
        "Tag cover focus: tag=%s source_size=%s focus=%s scale=%.4f "
        "visual_focus=(%.2f,%.2f) bounds=%s start=%.2f extent=%.2f",
        tag,
        (source_width, source_height),
        focus,
        required_scale,
        visual_focus[0],
        visual_focus[1],
        sector_bounds,
        start,
        extent,
    )
    # Resize once with a high-quality filter, then crop in the same canvas
    # coordinate system used by the focus transform.  Ceil keeps the raster
    # image at least as large as the mathematically required bounds.
    scaled_size = (
        max(1, math.ceil(source_width * required_scale)),
        max(1, math.ceil(source_height * required_scale)),
    )
    scaled = image.resize(scaled_size, Image.Resampling.LANCZOS)
    # Account for integer raster dimensions so the anchor remains at the
    # same output location even when the effective raster scale is rounded.
    raster_scale_x = scaled.width / source_width
    raster_scale_y = scaled.height / source_height
    crop_left = round(
        output_origin[0] - image_origin[0] + focus_x * (raster_scale_x - required_scale)
    )
    crop_top = round(
        output_origin[1] - image_origin[1] + focus_y * (raster_scale_y - required_scale)
    )
    return scaled.crop((crop_left, crop_top, crop_left + size[0], crop_top + size[1]))


async def generate_statistics_image(gallery: Gallery) -> Path | None:
    """Generate the existing Pillow statistics design from SQLite queries.

    Args:
        gallery: Initialized gallery instance.

    Returns:
        Generated PNG path, or None when the gallery has no images.
    """
    rows = await gallery.database.list_images()
    if not rows:
        return None
    counts: dict[str, int] = {}
    tag_images: dict[str, dict[str, Any] | None] = {}
    tag_focuses: dict[str, tuple[float, float] | None] = {}
    tag_face_radii: dict[str, float] = {}
    custom_cover_paths: dict[str, Path] = {}
    for display_name, english_tag in TAG_MAPPING.items():
        counts[display_name] = await gallery.database.count_for_tag(english_tag)
        tag_images[display_name] = await gallery.database.random_image_for_tag(
            english_tag
        )
        tag_focuses[display_name] = None
        tag_face_radii[display_name] = 40.0
        cover_config = TAG_COVERS.get(display_name)
        if cover_config is None:
            continue
        cover_path = Path(cover_config["path"])
        if not cover_path.is_file():
            logger.warning(
                "Statistics cover for tag %s does not exist: %s",
                display_name,
                cover_path,
            )
            continue
        try:
            with Image.open(cover_path) as cover_image:
                cover_image.verify()
        except (OSError, UnidentifiedImageError):
            logger.warning(
                "Statistics cover for tag %s cannot be read: %s",
                display_name,
                cover_path,
            )
            continue
        custom_cover_paths[display_name] = cover_path
        configured_focus = cover_config.get("face")
        configured_radius = cover_config.get("face_radius", 40.0)
        try:
            configured_radius = float(configured_radius)
            if not math.isfinite(configured_radius) or configured_radius < 0:
                raise ValueError("face_radius must be finite and non-negative")
            tag_face_radii[display_name] = configured_radius
        except (TypeError, ValueError, OverflowError):
            logger.warning(
                "Invalid statistics cover face radius for tag %s; using default",
                display_name,
            )
        if configured_focus is not None:
            try:
                focus = (float(configured_focus[0]), float(configured_focus[1]))
                if not all(math.isfinite(value) for value in focus):
                    raise ValueError("focus must be finite")
                tag_focuses[display_name] = focus
            except (TypeError, ValueError, IndexError, OverflowError):
                logger.warning(
                    "Invalid statistics cover focus for tag %s; using normal cover",
                    display_name,
                )
    pie_total = sum(counts.values())
    # Per-tag counts overlap, so the displayed total must be the distinct
    # number of images carrying at least one of the eight tags.
    distinct_total = await gallery.database.count_distinct_for_tags(
        list(TAG_MAPPING.values())
    )
    background_row = random.choice(rows)
    canvas_size = (1200, 850)
    with Image.open(background_row["file_path"]) as source:
        background = _cover(source, canvas_size)
    canvas = background.convert("RGBA")
    canvas.alpha_composite(Image.new("RGBA", canvas_size, (255, 255, 255, 190)))
    draw = ImageDraw.Draw(canvas)
    if TITLE_IMAGE_PATH.is_file():
        with Image.open(TITLE_IMAGE_PATH) as source:
            title = source.convert("RGBA")
        rgb = title.convert("RGB")
        white_distance = ImageChops.difference(
            rgb, Image.new("RGB", title.size, "white")
        )
        non_white = white_distance.convert("L").point(
            lambda value: 0 if value < 20 else 255
        )
        title.putalpha(ImageChops.multiply(title.getchannel("A"), non_white))
        title_width = 540
        title_height = max(1, round(title.height * title_width / title.width))
        title = title.resize((title_width, title_height), Image.Resampling.LANCZOS)
        title_alpha = title.getchannel("A")
        outline_alpha = title_alpha.filter(ImageFilter.MaxFilter(7))
        outline = Image.new("RGBA", title.size, (255, 255, 255, 0))
        outline.putalpha(outline_alpha)
        canvas.alpha_composite(outline, (60, 42))
        canvas.alpha_composite(title, (60, 42))
    draw.text(
        (950, 805),
        "Image Created By 咪啪~",
        fill=(35, 35, 35),
        font=_font(20, font_path=META_FONT_PATH),
    )
    draw.text(
        (750, 155),
        f"总计 {distinct_total} 张",
        fill=(35, 35, 35),
        font=_font(40, True, META_FONT_PATH),
    )

    center, radius = (330, 425), 260
    start = -90.0
    boundaries: list[tuple[float, float, tuple[int, int, int]]] = []
    for tag in TAG_MAPPING:
        count = counts[tag]
        if not count or not pie_total:
            continue
        extent = count / pie_total * 360
        mask = Image.new("L", canvas_size, 0)
        ImageDraw.Draw(mask).pieslice(
            (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            ),
            start,
            start + extent,
            fill=255,
        )
        image = tag_images[tag]
        image_path = custom_cover_paths.get(tag)
        is_custom = image_path is not None
        if image_path is None and image:
            image_path = Path(image["file_path"])
        if image_path and image_path.is_file():
            try:
                with Image.open(image_path) as source:
                    if is_custom and tag_focuses[tag] is not None:
                        source_image = _cover_with_focus(
                            source,
                            (radius * 2, radius * 2),
                            tag_focuses[tag],
                            canvas_center=center,
                            canvas_radius=radius,
                            start=start,
                            extent=extent,
                            face_radius=tag_face_radii[tag],
                            tag=tag,
                        ).convert("RGBA")
                    else:
                        source_image = _cover(source, (radius * 2, radius * 2)).convert(
                            "RGBA"
                        )
            except (OSError, UnidentifiedImageError):
                if is_custom and image:
                    logger.warning(
                        "Statistics cover for tag %s failed while rendering; "
                        "falling back to database image",
                        tag,
                    )
                    image_path = Path(image["file_path"])
                    is_custom = False
                    try:
                        with Image.open(image_path) as source:
                            source_image = _cover(
                                source, (radius * 2, radius * 2)
                            ).convert("RGBA")
                    except (OSError, UnidentifiedImageError):
                        source_image = None
                else:
                    source_image = None
            if source_image is not None:
                layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
                layer.paste(
                    source_image,
                    (center[0] - radius, center[1] - radius),
                    mask.crop(
                        (
                            center[0] - radius,
                            center[1] - radius,
                            center[0] + radius,
                            center[1] + radius,
                        )
                    ),
                )
                canvas.alpha_composite(layer)
        boundaries.append((start, extent, TAG_COLORS[tag]))
        start += extent
    box = (
        center[0] - radius,
        center[1] - radius,
        center[0] + radius,
        center[1] + radius,
    )
    for angle, extent, outline_color in boundaries:
        radians = math.radians(angle)
        draw.line(
            (
                center[0],
                center[1],
                center[0] + radius * math.cos(radians),
                center[1] + radius * math.sin(radians),
            ),
            fill=outline_color,
            width=4,
        )
        draw.arc(box, angle, angle + extent, fill=outline_color, width=4)
    state = gallery.load_state()
    y = 220
    for tag in TAG_MAPPING:
        count = counts[tag]
        percent = count / pie_total * 100 if pie_total else 0
        draw.ellipse((750, y, 784, y + 34), fill=TAG_COLORS[tag])
        draw.text(
            (802, y - 2),
            tag,
            fill=(45, 45, 45),
            font=_font(24, font_path=TAG_FONT_PATH),
        )
        line = f"{count} 张 · {percent:.1f}%"
        draw.text(
            (802, y + 30),
            line,
            fill=(85, 85, 85),
            font=_font(19, font_path=TAG_FONT_PATH),
        )
        english_tag = TAG_MAPPING[tag]
        added = state.get("last_added_by_tag", {}).get(english_tag)
        if isinstance(added, int) and added > 0:
            offset = draw.textlength(line, font=_font(19, font_path=TAG_FONT_PATH))
            draw.text(
                (802 + offset + 12, y + 30),
                f"(+{added})",
                fill=(208, 55, 55),
                font=_font(19, font_path=TAG_FONT_PATH),
            )
        y += 64
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latest = state.get("last_update", "暂无记录")
    failures = state.get("consecutive_failures")
    last_error = state.get("last_error")
    draw.text(
        (60, 770),
        "统计时间：",
        fill=(70, 70, 70),
        font=_font(18, font_path=META_FONT_PATH),
    )
    draw.text(
        (150, 770),
        f"{now}",
        fill=(70, 70, 70),
        font=_font(18, font_path=TAG_FONT_PATH),
    )
    draw.text(
        (60, 805),
        "上次成功：",
        fill=(70, 70, 70),
        font=_font(18, font_path=META_FONT_PATH),
    )
    draw.text(
        (150, 805),
        f"{latest}",
        fill=(70, 70, 70),
        font=_font(18, font_path=TAG_FONT_PATH),
    )
    if isinstance(failures, int) and failures > 0:
        suffix = f"（连续失败 {failures} 次"
        if isinstance(last_error, str) and last_error:
            suffix += f"：{last_error[:40]}"
        suffix += "）"
        draw.text(
            (360, 805),
            suffix,
            fill=(200, 45, 45),
            font=_font(16, font_path=TAG_FONT_PATH),
        )
    output = gallery.output_dir / "statistics.png"
    canvas.convert("RGB").save(output, "PNG")
    return output
