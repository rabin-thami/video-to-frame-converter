"""Video metadata via FFprobe when available, with an OpenCV fallback."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2

from .config import (
    MODE_EVERY_FRAME,
    MODE_FRAME_INTERVAL,
    MODE_SCENE,
    MODE_TIME_INTERVAL,
)
from .errors import VideoError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float | None
    frame_count: int | None
    duration: float | None
    codec: str | None = None
    frame_count_is_estimate: bool = False
    source: str = "OpenCV"

    @property
    def resolution_label(self) -> str:
        if self.width and self.height:
            return f"{self.width} x {self.height}"
        return "Unknown"

    @property
    def fps_label(self) -> str:
        return f"{self.fps:.3f} fps" if self.fps else "Unknown"

    @property
    def frame_count_label(self) -> str:
        if self.frame_count is None:
            return "Unknown"
        suffix = " (estimated)" if self.frame_count_is_estimate else ""
        return f"{self.frame_count:,}{suffix}"


def _parse_rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            den = float(denominator)
            return float(numerator) / den if den else None
        rate = float(value)
        return rate if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _probe_ffprobe(path: Path) -> tuple[VideoInfo | None, str | None]:
    executable = shutil.which("ffprobe")
    if not executable:
        return None, None

    command = [
        executable,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,codec_name",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"FFprobe could not run: {exc}"
    if completed.returncode != 0:
        message = (completed.stderr or "unknown error").strip().splitlines()[-1]
        return None, f"FFprobe failed: {message}"

    try:
        payload = json.loads(completed.stdout)
        stream = (payload.get("streams") or [{}])[0]
    except (json.JSONDecodeError, IndexError):
        return None, "FFprobe returned unreadable metadata."

    duration = _float_or_none((payload.get("format") or {}).get("duration"))
    if duration is None:
        duration = _float_or_none(stream.get("duration"))
    fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
    frame_count = None
    try:
        raw_count = int(stream.get("nb_frames"))
        if raw_count > 0:
            frame_count = raw_count
    except (TypeError, ValueError):
        pass

    estimated = False
    if frame_count is None and duration and fps:
        frame_count = round(duration * fps)
        estimated = True

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        return None, "FFprobe did not report a usable video stream."

    return (
        VideoInfo(
            path=path,
            width=width,
            height=height,
            fps=fps,
            frame_count=frame_count,
            duration=duration,
            codec=stream.get("codec_name"),
            frame_count_is_estimate=estimated,
            source="FFprobe",
        ),
        None,
    )


def _probe_opencv(path: Path) -> VideoInfo:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise VideoError(
                "The video could not be opened. It may be corrupted, use an unsupported codec, "
                "or still be copying. Try another file or install FFmpeg."
            )
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        fps = float(fps) if fps and math.isfinite(fps) and fps > 0 else None
        raw_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        frame_count = int(raw_count) if raw_count and math.isfinite(raw_count) and raw_count > 0 else None
        duration = (frame_count / fps) if frame_count and fps else None
        if width <= 0 or height <= 0:
            raise VideoError(
                "The video has no readable video stream. It may be corrupted or contain only audio."
            )
        return VideoInfo(
            path=path,
            width=width,
            height=height,
            fps=fps,
            frame_count=frame_count,
            duration=duration,
            frame_count_is_estimate=duration is None and frame_count is None,
            source="OpenCV",
        )
    finally:
        capture.release()


def probe_video(path: Path) -> VideoInfo:
    """Read video metadata, raising VideoError when the file cannot be used."""
    path = Path(path)
    ffprobe_info, warning = _probe_ffprobe(path)
    if warning:
        logger.warning("%s", warning)
    try:
        opencv_info = _probe_opencv(path)
    except VideoError:
        if ffprobe_info is None:
            raise
        opencv_info = None

    if ffprobe_info is None:
        if opencv_info is None:
            raise VideoError("The video metadata could not be read.")
        logger.info("Using OpenCV metadata for %s", path)
        return opencv_info

    # Fill gaps found by FFprobe with OpenCV values when possible.
    if opencv_info is not None and (not ffprobe_info.duration or not ffprobe_info.fps):
        return VideoInfo(
            path=path,
            width=ffprobe_info.width or opencv_info.width,
            height=ffprobe_info.height or opencv_info.height,
            fps=ffprobe_info.fps or opencv_info.fps,
            frame_count=ffprobe_info.frame_count or opencv_info.frame_count,
            duration=ffprobe_info.duration or opencv_info.duration,
            codec=ffprobe_info.codec,
            frame_count_is_estimate=ffprobe_info.frame_count_is_estimate,
            source=ffprobe_info.source,
        )
    return ffprobe_info


def estimate_candidate_frames(
    info: VideoInfo,
    mode: str,
    interval_seconds: float,
    frame_interval: int,
    start_time: float,
    end_time: float | None,
) -> int | None:
    """Rough candidate count for the GUI. Never a promise of retained images."""
    duration = info.duration
    if duration is None:
        return None
    end = min(end_time, duration) if end_time is not None else duration
    span = max(0.0, end - start_time)
    if span <= 0:
        return 0

    if mode == MODE_TIME_INTERVAL:
        if interval_seconds <= 0:
            return None
        return max(0, math.floor(span / interval_seconds + 1e-9) + 1)

    total_frames = info.frame_count
    if total_frames is None and info.fps:
        total_frames = round(duration * info.fps)
    if total_frames is None:
        return None
    frames_in_range = total_frames * (span / duration) if duration else total_frames
    if mode == MODE_FRAME_INTERVAL:
        if frame_interval < 1:
            return None
        return max(0, math.ceil(frames_in_range / frame_interval))
    if mode in (MODE_EVERY_FRAME, MODE_SCENE):
        return max(0, round(frames_in_range))
    return None
