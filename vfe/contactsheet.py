"""Contact sheet generation with readable thumbnails and timestamp captions."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import ContactSheetSettings
from .images import FrameRecord
from .utils import format_timestamp, safe_stem

logger = logging.getLogger(__name__)

BACKGROUND = "#eef1f5"
CELL = "#ffffff"
TEXT = "#1f2937"
MUTED = "#667085"
ACCENT = "#1d4ed8"
GAP = 14
MARGIN = 18
CAPTION_HEIGHT = 40
HEADER_HEIGHT = 58
JPEG_QUALITY = 90


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        [
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10 fallback
        return ImageFont.load_default()


def _ellipsize(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle] + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + ellipsis


def _build_sheet(
    chunk: list[FrameRecord],
    sheet_index: int,
    sheet_count: int,
    total_records: int,
    video_name: str,
    first_number: int,
    settings: ContactSheetSettings,
) -> Image.Image:
    thumb_w = settings.thumbnail_width
    thumb_h = max(90, round(thumb_w * 9 / 16))
    rows = math.ceil(len(chunk) / settings.columns)
    cell_w = thumb_w
    cell_h = thumb_h + CAPTION_HEIGHT
    grid_w = settings.columns * cell_w + (settings.columns - 1) * GAP
    sheet_w = MARGIN * 2 + grid_w
    sheet_h = HEADER_HEIGHT + MARGIN + rows * cell_h + (rows - 1) * GAP + MARGIN

    sheet = Image.new("RGB", (sheet_w, sheet_h), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    title_font = _load_font(19, bold=True)
    subtitle_font = _load_font(12)
    caption_font = _load_font(12)
    time_font = _load_font(11)

    draw.text((MARGIN, 14), f"Contact sheet {sheet_index} of {sheet_count}", fill=TEXT, font=title_font)
    subtitle = f"{video_name}  ·  frames {first_number}–{first_number + len(chunk) - 1} of {total_records}"
    draw.text(
        (MARGIN, 37),
        _ellipsize(subtitle, subtitle_font, sheet_w - MARGIN * 2, draw),
        fill=MUTED,
        font=subtitle_font,
    )

    for position, record in enumerate(chunk):
        row, column = divmod(position, settings.columns)
        x = MARGIN + column * (cell_w + GAP)
        y = HEADER_HEIGHT + MARGIN + row * (cell_h + GAP)
        draw.rounded_rectangle(
            (x, y, x + cell_w, y + cell_h), radius=5, fill=CELL, outline="#d9dee7"
        )
        image_x = x
        image_y = y
        source_path = (
            record.original_path
            if record.original_path is not None and record.original_path.exists()
            else record.path
        )
        try:
            with Image.open(source_path) as source:
                image = source.convert("RGB")
            image = ImageOps.contain(image, (thumb_w, thumb_h), Image.Resampling.LANCZOS)
            image_x = x + (thumb_w - image.width) // 2
            image_y = y + (thumb_h - image.height) // 2
            sheet.paste(image, (image_x, image_y))
            image.close()
        except (OSError, ValueError):
            logger.warning("Contact sheet could not open %s", record.path)
            draw.text((x + 8, y + 8), "Image unavailable", fill="#b91c1c", font=caption_font)

        filename = _ellipsize(record.path.name, caption_font, thumb_w - 12, draw)
        draw.text((x + 6, y + thumb_h + 5), filename, fill=TEXT, font=caption_font)
        timestamp = format_timestamp(record.timestamp)
        draw.text((x + 6, y + thumb_h + 22), timestamp, fill=ACCENT, font=time_font)

    return sheet


def create_contact_sheets(
    job_dir: Path,
    video_name: str,
    records: list[FrameRecord],
    settings: ContactSheetSettings,
) -> list[Path]:
    """Write one or more contact sheet JPEGs and return their paths."""
    if not records:
        return []
    sheet_dir = job_dir / "contact_sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(video_name)
    chunks = [
        records[index : index + settings.max_thumbnails]
        for index in range(0, len(records), settings.max_thumbnails)
    ]
    paths: list[Path] = []
    for index, chunk in enumerate(chunks, start=1):
        sheet = _build_sheet(
            chunk=chunk,
            sheet_index=index,
            sheet_count=len(chunks),
            total_records=len(records),
            video_name=video_name,
            first_number=chunk[0].index,
            settings=settings,
        )
        path = sheet_dir / f"{stem}_contact_sheet_{index:03d}.jpg"
        try:
            sheet.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        finally:
            sheet.close()
        paths.append(path)
        logger.info("Contact sheet written: %s", path)
    return paths
