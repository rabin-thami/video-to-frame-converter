"""Small shared helpers for formatting times and filenames."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .errors import ValidationError

_INVALID_FILENAME = re.compile(r"[^\w.-]+", re.UNICODE)


def parse_time(value: str, field_name: str = "time") -> float:
    """Parse '12', '90.5' or 'HH:MM:SS(.ms)' into seconds."""
    text = value.strip()
    if not text:
        raise ValidationError(f"Please enter a {field_name}.")
    try:
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                hours, minutes, seconds = 0.0, float(parts[0]), float(parts[1])
            elif len(parts) == 3:
                hours, minutes, seconds = float(parts[0]), float(parts[1]), float(parts[2])
            else:
                raise ValueError
            if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
                raise ValueError
            return hours * 3600 + minutes * 60 + seconds
        seconds = float(text)
    except ValueError as exc:
        raise ValidationError(
            f"Invalid {field_name} '{value}'. Use seconds (12.5) or HH:MM:SS (00:00:12.5)."
        ) from exc
    if seconds < 0:
        raise ValidationError(f"The {field_name} cannot be negative.")
    return seconds


def format_timestamp(seconds: float, milliseconds: bool = True) -> str:
    """Format seconds as HH:MM:SS or HH:MM:SS.mmm."""
    seconds = max(0.0, float(seconds))
    if milliseconds:
        total_milliseconds = round(seconds * 1000)
        hours, remainder = divmod(total_milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        whole, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{whole:02d}.{millis:03d}"
    total_seconds = round(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, whole = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{whole:02d}"


def format_duration(seconds: float | None) -> str:
    """Short human duration used by the GUI."""
    if seconds is None:
        return "Unknown"
    return format_timestamp(seconds, milliseconds=False)


def safe_stem(filename: str, max_length: int = 60) -> str:
    """Turn a video filename stem into a safe, portable folder/file prefix."""
    stem = Path(filename).stem
    stem = _INVALID_FILENAME.sub("_", stem).strip(" ._-") or "video"
    return stem[:max_length].rstrip(" ._-") or "video"


def open_folder(path: Path) -> None:
    """Open a folder in the platform's file manager."""
    if sys.platform.startswith("win"):
        import os

        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
