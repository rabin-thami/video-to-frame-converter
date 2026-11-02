"""JSON extraction report builder."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import MODE_LABELS, ExtractionSettings
from .images import FrameRecord
from .probe import VideoInfo
from .utils import format_timestamp

SIMILARITY_METHOD = (
    "pHash + dHash + SSIM + HSV histogram; compares each candidate with the "
    "current group representative plus strict repeat checks; meaningful-motion "
    "and hard-scene-cut guards"
)


def _relative(path: Path, job_dir: Path) -> str:
    try:
        return path.relative_to(job_dir).as_posix()
    except ValueError:
        return str(path)


def write_metadata(
    job_dir: Path,
    settings: ExtractionSettings,
    info: VideoInfo,
    records: list[FrameRecord],
    candidate_frames: int,
    retained_frames: int,
    removed_frames: int,
    errors: list[str],
    warnings: list[str],
    contact_sheets: list[Path],
    cancelled: bool,
    elapsed_seconds: float,
    effective_start: float,
    effective_end: float | None,
) -> Path:
    """Write metadata.json atomically and return its path."""
    removed_percent = (removed_frames / candidate_frames * 100.0) if candidate_frames else 0.0
    similarity_enabled = settings.similarity.enabled or settings.mode == "scene"

    frames = []
    for record in records:
        entry = {
            "frame_number": record.index,
            "filename": _relative(record.path, job_dir),
            "timestamp_seconds": round(record.timestamp, 3),
            "timestamp": format_timestamp(record.timestamp),
        }
        if record.original_path is not None:
            entry["original_filename"] = _relative(record.original_path, job_dir)
        frames.append(entry)

    payload = {
        "application": "Video Frame Extractor",
        "version": __version__,
        "extraction_date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cancelled": cancelled,
        "video": {
            "filename": info.path.name,
            "path": str(info.path),
            "duration_seconds": round(info.duration, 3) if info.duration else None,
            "duration": format_timestamp(info.duration) if info.duration else None,
            "width": info.width,
            "height": info.height,
            "frame_rate": round(info.fps, 3) if info.fps else None,
            "total_frames": info.frame_count,
            "total_frames_estimated": info.frame_count_is_estimate,
            "codec": info.codec,
            "metadata_source": info.source,
        },
        "extraction": {
            "method": settings.mode,
            "method_label": MODE_LABELS.get(settings.mode, settings.mode),
            "start_seconds": round(effective_start, 3),
            "start": format_timestamp(effective_start),
            "end_seconds": round(effective_end, 3) if effective_end is not None else None,
            "end": format_timestamp(effective_end) if effective_end is not None else None,
            "time_interval_seconds": (
                settings.interval_seconds if settings.mode == "time_interval" else None
            ),
            "frame_interval": (
                settings.frame_interval if settings.mode == "frame_interval" else None
            ),
            "image_format": settings.image_format.upper(),
            "quality": settings.quality if settings.image_format != "png" else None,
            "resize": {
                "mode": settings.resize.mode,
                "max_width": settings.resize.width,
                "max_height": settings.resize.height,
                "save_original_resolution_copies": settings.resize.save_originals,
            },
            "elapsed_seconds": round(elapsed_seconds, 2),
        },
        "similarity_filtering": {
            "enabled": similarity_enabled,
            "method": SIMILARITY_METHOD if similarity_enabled else None,
            "threshold_percent": settings.similarity.threshold,
            "effective_threshold_percent": (
                settings.similarity.effective_threshold if similarity_enabled else None
            ),
            "preserve_more": settings.similarity.preserve_more,
            "motion_sensitivity": settings.similarity.motion_sensitivity,
            "effective_motion_sensitivity": settings.similarity.effective_motion_sensitivity,
        },
        "results": {
            "candidate_frames": candidate_frames,
            "retained_images": retained_frames,
            "removed_frames": removed_frames,
            "removed_percent": round(removed_percent, 2),
            "contact_sheets": [_relative(path, job_dir) for path in contact_sheets],
        },
        "frames": frames,
        "errors": errors,
        "warnings": warnings,
    }

    path = job_dir / "metadata.json"
    temporary = path.with_name(path.name + ".part")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return path
