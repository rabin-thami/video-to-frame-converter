"""Settings dataclasses, shared constants and settings validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .errors import ValidationError

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpeg", ".mpg"}

MODE_EVERY_FRAME = "every_frame"
MODE_TIME_INTERVAL = "time_interval"
MODE_FRAME_INTERVAL = "frame_interval"
MODE_SCENE = "scene"
MODES = (MODE_EVERY_FRAME, MODE_TIME_INTERVAL, MODE_FRAME_INTERVAL, MODE_SCENE)

MODE_LABELS = {
    MODE_EVERY_FRAME: "Every frame",
    MODE_TIME_INTERVAL: "Fixed time interval",
    MODE_FRAME_INTERVAL: "Fixed frame interval",
    MODE_SCENE: "Automatic (scene-aware)",
}

IMAGE_FORMATS = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
IMAGE_FORMAT_LABELS = {"PNG (lossless)": "png", "JPEG": "jpeg", "WebP": "webp"}

RESIZE_ORIGINAL = "original"
RESIZE_MAX_WIDTH = "max_width"
RESIZE_MAX_HEIGHT = "max_height"
RESIZE_CUSTOM = "custom"
RESIZE_MODES = (RESIZE_ORIGINAL, RESIZE_MAX_WIDTH, RESIZE_MAX_HEIGHT, RESIZE_CUSTOM)
RESIZE_LABELS = {
    RESIZE_ORIGINAL: "Original resolution",
    RESIZE_MAX_WIDTH: "Maximum width",
    RESIZE_MAX_HEIGHT: "Maximum height",
    RESIZE_CUSTOM: "Custom box (keeps aspect ratio)",
}


@dataclass(frozen=True)
class SimilaritySettings:
    """Online duplicate / near-duplicate filtering settings."""

    enabled: bool = True
    threshold: float = 90.0
    preserve_more: bool = False
    # Fraction of significantly changed pixels needed to override a duplicate match.
    motion_sensitivity: float = 0.04

    @property
    def effective_threshold(self) -> float:
        """Threshold in percent. 'Preserve more' requires higher similarity to drop a frame."""
        bump = 5.0 if self.preserve_more else 0.0
        return max(0.0, min(100.0, self.threshold + bump))

    @property
    def effective_motion_sensitivity(self) -> float:
        """'Preserve more' also reacts to smaller subject movement."""
        factor = 0.6 if self.preserve_more else 1.0
        return max(0.005, self.motion_sensitivity * factor)


@dataclass(frozen=True)
class ResizeSettings:
    """Output resizing. Images are never enlarged."""

    mode: str = RESIZE_ORIGINAL
    width: int | None = None
    height: int | None = None
    save_originals: bool = False


@dataclass(frozen=True)
class ContactSheetSettings:
    enabled: bool = True
    columns: int = 4
    max_thumbnails: int = 40
    thumbnail_width: int = 360


@dataclass(frozen=True)
class ExtractionSettings:
    """Everything needed to run one extraction job."""

    video_path: Path
    output_root: Path
    mode: str = MODE_TIME_INTERVAL
    interval_seconds: float = 1.0
    frame_interval: int = 30
    start_time: float = 0.0
    end_time: float | None = None
    image_format: str = "png"
    quality: int = 95
    similarity: SimilaritySettings = field(default_factory=SimilaritySettings)
    resize: ResizeSettings = field(default_factory=ResizeSettings)
    contact_sheet: ContactSheetSettings = field(default_factory=ContactSheetSettings)
    write_metadata: bool = True


def validate_settings(settings: ExtractionSettings) -> None:
    """Raise ValidationError with a helpful message for invalid settings."""
    video = settings.video_path
    if not video.exists():
        raise ValidationError(f"The video file does not exist:\n{video}")
    if not video.is_file():
        raise ValidationError(f"The selected video path is not a file:\n{video}")
    if video.suffix.lower() not in VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(ext.lstrip(".").upper() for ext in VIDEO_EXTENSIONS))
        raise ValidationError(f"Unsupported video format '{video.suffix or '(none)'}'.\nSupported: {supported}")
    if video.stat().st_size == 0:
        raise ValidationError(f"The video file is empty:\n{video}")

    if not settings.output_root:
        raise ValidationError("Please choose an output folder.")
    output = settings.output_root
    if output.exists() and not output.is_dir():
        raise ValidationError(f"The output path is not a folder:\n{output}")

    if settings.mode not in MODES:
        raise ValidationError(f"Unknown extraction method: {settings.mode}")
    if settings.interval_seconds <= 0:
        raise ValidationError("The time interval must be greater than 0 seconds.")
    if settings.interval_seconds > 86400:
        raise ValidationError("The time interval is too large (maximum 24 hours).")
    if settings.frame_interval < 1:
        raise ValidationError("The frame interval must be 1 or greater.")
    if settings.start_time < 0:
        raise ValidationError("The start time cannot be negative.")
    if settings.end_time is not None and settings.end_time <= settings.start_time:
        raise ValidationError("The end time must be later than the start time.")

    if settings.image_format not in IMAGE_FORMATS:
        raise ValidationError(f"Unsupported image format: {settings.image_format}")
    if not 1 <= settings.quality <= 100:
        raise ValidationError("Image quality must be between 1 and 100.")

    similarity = settings.similarity
    if not 0 <= similarity.threshold <= 100:
        raise ValidationError("The similarity threshold must be between 0 and 100.")
    if not 0 <= similarity.motion_sensitivity <= 1:
        raise ValidationError("The motion sensitivity must be between 0 and 1.")

    resize = settings.resize
    if resize.mode not in RESIZE_MODES:
        raise ValidationError(f"Unknown resize mode: {resize.mode}")
    if resize.mode in (RESIZE_MAX_WIDTH, RESIZE_CUSTOM) and (resize.width is None or resize.width < 1):
        raise ValidationError("Please enter a maximum width greater than 0.")
    if resize.mode in (RESIZE_MAX_HEIGHT, RESIZE_CUSTOM) and (resize.height is None or resize.height < 1):
        raise ValidationError("Please enter a maximum height greater than 0.")
    if resize.width is not None and resize.width > 20000:
        raise ValidationError("The output width is too large (maximum 20000 pixels).")
    if resize.height is not None and resize.height > 20000:
        raise ValidationError("The output height is too large (maximum 20000 pixels).")

    sheet = settings.contact_sheet
    if not 1 <= sheet.columns <= 12:
        raise ValidationError("Contact sheet columns must be between 1 and 12.")
    if not 1 <= sheet.max_thumbnails <= 1000:
        raise ValidationError("Thumbnails per contact sheet must be between 1 and 1000.")
    if not 100 <= sheet.thumbnail_width <= 1200:
        raise ValidationError("Contact sheet thumbnail width must be between 100 and 1200 pixels.")
    if sheet.max_thumbnails < sheet.columns:
        raise ValidationError("Thumbnails per contact sheet must be at least the number of columns.")
