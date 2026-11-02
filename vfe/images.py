"""Image resizing and reliable, sequential frame export."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .config import (
    IMAGE_FORMATS,
    RESIZE_CUSTOM,
    RESIZE_MAX_HEIGHT,
    RESIZE_MAX_WIDTH,
    RESIZE_ORIGINAL,
    ExtractionSettings,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameRecord:
    index: int
    path: Path
    timestamp: float
    original_path: Path | None = None


def compute_output_size(
    width: int,
    height: int,
    resize_mode: str,
    box_width: int | None,
    box_height: int | None,
) -> tuple[int, int]:
    """Return the output size, preserving aspect ratio and never enlarging."""
    if resize_mode == RESIZE_ORIGINAL:
        return width, height

    if resize_mode == RESIZE_MAX_WIDTH and box_width:
        scale = min(1.0, box_width / width)
    elif resize_mode == RESIZE_MAX_HEIGHT and box_height:
        scale = min(1.0, box_height / height)
    elif resize_mode == RESIZE_CUSTOM and box_width and box_height:
        scale = min(1.0, box_width / width, box_height / height)
    else:
        return width, height

    return max(1, round(width * scale)), max(1, round(height * scale))


def _pil_resize(image: Image.Image, width: int, height: int) -> Image.Image:
    if image.size == (width, height):
        return image
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _save_image(image: Image.Image, path: Path, image_format: str, quality: int) -> None:
    """Save through a temporary file so interrupted writes never look complete."""
    temporary = path.with_name(path.name + ".part")
    try:
        if image_format == "png":
            image.save(temporary, format="PNG", compress_level=6)
        elif image_format == "jpeg":
            image.save(temporary, format="JPEG", quality=quality, optimize=False)
        elif image_format == "webp":
            image.save(temporary, format="WEBP", quality=quality, method=4)
        else:
            raise ValueError(f"Unsupported image format: {image_format}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


class FrameWriter:
    """Writes retained frames to the job folder with zero-padded names."""

    def __init__(self, job_dir: Path, video_stem: str, settings: ExtractionSettings):
        self.job_dir = job_dir
        self.video_stem = video_stem
        self.settings = settings
        self.frame_dir = job_dir / "frames"
        self.original_dir = job_dir / "originals"
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self.extension = IMAGE_FORMATS[settings.image_format]
        self.sequence = 0
        self.warnings: list[str] = []

    def save(self, frame_bgr: np.ndarray, timestamp: float) -> FrameRecord:
        """Resize (if requested) and write one representative frame."""
        self.sequence += 1
        filename = f"{self.video_stem}_frame_{self.sequence:06d}{self.extension}"
        path = self.frame_dir / filename

        height, width = frame_bgr.shape[:2]
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        output_width, output_height = compute_output_size(
            width,
            height,
            self.settings.resize.mode,
            self.settings.resize.width,
            self.settings.resize.height,
        )
        resized = _pil_resize(image, output_width, output_height)

        try:
            try:
                _save_image(resized, path, self.settings.image_format, self.settings.quality)
            except (OSError, ValueError) as exc:
                raise OSError(f"Could not write image '{path}': {exc}") from exc

            original_path: Path | None = None
            needs_original_copy = (
                (output_width, output_height) != (width, height)
                and self.settings.resize.save_originals
            )
            if needs_original_copy:
                self.original_dir.mkdir(parents=True, exist_ok=True)
                original_path = self.original_dir / filename
                try:
                    _save_image(
                        image, original_path, self.settings.image_format, self.settings.quality
                    )
                except (OSError, ValueError) as exc:
                    message = (
                        f"Could not write original-resolution copy '{original_path}': {exc}"
                    )
                    original_path = None
                    self.warnings.append(message)
                    logger.warning(message)
        finally:
            if resized is not image:
                resized.close()
            image.close()

        return FrameRecord(
            index=self.sequence,
            path=path,
            timestamp=timestamp,
            original_path=original_path,
        )
