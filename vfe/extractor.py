"""Extraction orchestration and online representative-frame selection."""

from __future__ import annotations

import logging
import math
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .config import (
    MODE_EVERY_FRAME,
    MODE_FRAME_INTERVAL,
    MODE_SCENE,
    MODE_TIME_INTERVAL,
    ExtractionSettings,
    validate_settings,
)
from .contactsheet import create_contact_sheets
from .errors import ExtractionCancelled, ValidationError, VideoError
from .images import FrameRecord, FrameWriter
from .probe import VideoInfo, probe_video
from .report import write_metadata
from .similarity import (
    FrameSignature,
    analyze_frame,
    compare_signatures,
    is_likely_scene_cut,
)
from .utils import format_timestamp, safe_stem

logger = logging.getLogger(__name__)

MAX_RECENT_REPRESENTATIVES = 256


@dataclass(frozen=True)
class ExtractionProgress:
    fraction: float | None
    candidates: int
    retained: int
    removed: int
    elapsed_seconds: float
    eta_seconds: float | None
    status: str


@dataclass
class ExtractionResult:
    job_dir: Path
    video_info: VideoInfo
    records: list[FrameRecord] = field(default_factory=list)
    candidate_frames: int = 0
    retained_frames: int = 0
    removed_frames: int = 0
    contact_sheets: list[Path] = field(default_factory=list)
    metadata_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    elapsed_seconds: float = 0.0


class ExtractionControl:
    """Thread-safe pause / resume / cancel control used by the GUI."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._paused = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancel.set()
        self._paused.clear()

    def check(self) -> None:
        """Block while paused; raise ExtractionCancelled when cancelled."""
        if self._cancel.is_set():
            raise ExtractionCancelled("Extraction cancelled by the user.")
        while self._paused.is_set():
            if self._cancel.is_set():
                raise ExtractionCancelled("Extraction cancelled by the user.")
            time.sleep(0.1)


@dataclass
class _PendingRepresentative:
    frame: np.ndarray
    signature: FrameSignature
    timestamp: float
    shadow: bool = False


@dataclass
class _Counters:
    candidates: int = 0
    retained: int = 0
    removed: int = 0


class _ProgressReporter:
    def __init__(
        self,
        callback: Callable[[ExtractionProgress], None] | None,
        started_at: float,
        start_time: float,
        end_time: float | None,
    ) -> None:
        self.callback = callback
        self.started_at = started_at
        self.start_time = start_time
        self.end_time = end_time
        self.last_emit = 0.0

    def report(
        self,
        counters: _Counters,
        timestamp: float,
        status: str,
        force: bool = False,
    ) -> None:
        if self.callback is None:
            return
        now = time.monotonic()
        if not force and now - self.last_emit < 0.15:
            return
        self.last_emit = now
        elapsed = now - self.started_at
        fraction: float | None = None
        if self.end_time is not None and self.end_time > self.start_time:
            fraction = (timestamp - self.start_time) / (self.end_time - self.start_time)
            fraction = max(0.0, min(1.0, fraction))
        eta = None
        if fraction and fraction > 0.005 and not force:
            eta = max(0.0, elapsed / fraction - elapsed)
        self.callback(
            ExtractionProgress(
                fraction=fraction,
                candidates=counters.candidates,
                retained=counters.retained,
                removed=counters.removed,
                elapsed_seconds=elapsed,
                eta_seconds=eta,
                status=status,
            )
        )


def _create_job_dir(output_root: Path, stem: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{stem}_extracted_{timestamp}"
    candidate = output_root / base
    counter = 2
    while candidate.exists():
        candidate = output_root / f"{base}_{counter}"
        counter += 1
    candidate.mkdir(parents=True)
    return candidate


def _check_free_space(output_root: Path) -> None:
    probe_target = output_root
    while not probe_target.exists() and probe_target != probe_target.parent:
        probe_target = probe_target.parent
    try:
        free = shutil.disk_usage(probe_target).free
    except OSError:
        return
    if free < 100 * 1024 * 1024:
        raise ValidationError(
            f"Only {free / (1024 * 1024):.0f} MB of free disk space is available. "
            "Free some space or choose another output folder."
        )


def _open_capture(path: Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise VideoError(
            "The video could not be opened for extraction. It may be corrupted, "
            "use an unsupported codec, or still be copying."
        )
    return capture


def _iter_candidates(
    capture: cv2.VideoCapture,
    info: VideoInfo,
    settings: ExtractionSettings,
    control: ExtractionControl,
    reporter: _ProgressReporter,
    counters: _Counters,
    end_time: float | None,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield only the frames selected by the extraction method, in order."""
    fps = info.fps or capture.get(cv2.CAP_PROP_FPS)
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        fps = 0.0
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0
    frame_period = 1.0 / fps

    if settings.start_time > 0:
        capture.set(cv2.CAP_PROP_POS_MSEC, settings.start_time * 1000.0)

    next_time_target = settings.start_time
    frames_in_range = 0
    decoded_frames = 0
    last_timestamp = settings.start_time
    last_raw_timestamp = 0.0
    tolerance = frame_period * 0.51

    while True:
        control.check()
        ok, frame = capture.read()
        if not ok:
            break

        raw_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
        raw_timestamp = raw_ms / 1000.0 if raw_ms and math.isfinite(raw_ms) and raw_ms > 0 else None
        guessed_timestamp = settings.start_time + decoded_frames * frame_period
        decoded_frames += 1

        if (
            raw_timestamp is not None
            and raw_timestamp >= settings.start_time - frame_period
            and raw_timestamp >= last_raw_timestamp - frame_period
        ):
            timestamp = raw_timestamp
            last_raw_timestamp = max(last_raw_timestamp, raw_timestamp)
        else:
            timestamp = max(guessed_timestamp, last_timestamp + 1e-6)
        timestamp = max(timestamp, last_timestamp)
        last_timestamp = timestamp

        if timestamp < settings.start_time - tolerance:
            continue
        if end_time is not None and timestamp > end_time + tolerance:
            break

        in_range_index = frames_in_range
        frames_in_range += 1
        selected = False
        if settings.mode in (MODE_EVERY_FRAME, MODE_SCENE):
            selected = True
        elif settings.mode == MODE_TIME_INTERVAL:
            if timestamp + 1e-6 >= next_time_target:
                selected = True
                while next_time_target <= timestamp + 1e-6:
                    next_time_target += settings.interval_seconds
        elif settings.mode == MODE_FRAME_INTERVAL:
            selected = in_range_index % settings.frame_interval == 0

        if selected:
            counters.candidates += 1
            reporter.report(counters, timestamp, "Reading video and selecting frames")
            yield timestamp, frame


def _is_repeat_of_recent(
    signature: FrameSignature,
    recent: deque[FrameSignature],
    threshold_percent: float,
    motion_sensitivity: float,
) -> bool:
    """Strict check used only when a new visual group starts."""
    strict_threshold = max(0.96, min(0.995, threshold_percent / 100.0 + 0.01))
    for previous in reversed(recent):
        if (signature.phash ^ previous.phash).bit_count() > 8:
            continue
        if (signature.dhash ^ previous.dhash).bit_count() > 12:
            continue
        result = compare_signatures(previous, signature)
        if (
            result.score >= strict_threshold
            and result.changed_fraction < motion_sensitivity
        ):
            return True
    return False


def _save_pending(
    pending: _PendingRepresentative | None,
    writer: FrameWriter,
    counters: _Counters,
    records: list[FrameRecord],
    recent: deque[FrameSignature],
) -> None:
    if pending is None or pending.shadow:
        return
    record = writer.save(pending.frame, pending.timestamp)
    records.append(record)
    counters.retained += 1
    recent.append(pending.signature)
    logger.debug(
        "Retained frame %06d at %s", record.index, format_timestamp(record.timestamp)
    )


def _process_with_filtering(
    frames: Iterator[tuple[float, np.ndarray]],
    settings: ExtractionSettings,
    writer: FrameWriter,
    counters: _Counters,
    reporter: _ProgressReporter,
    records: list[FrameRecord],
    errors: list[str],
) -> None:
    threshold = settings.similarity.effective_threshold
    motion = settings.similarity.effective_motion_sensitivity
    recent: deque[FrameSignature] = deque(maxlen=MAX_RECENT_REPRESENTATIVES)
    pending: _PendingRepresentative | None = None
    previous_signature: FrameSignature | None = None

    def start_group(frame: np.ndarray, signature: FrameSignature, timestamp: float) -> None:
        nonlocal pending
        shadow = _is_repeat_of_recent(signature, recent, threshold, motion)
        pending = _PendingRepresentative(
            frame=frame, signature=signature, timestamp=timestamp, shadow=shadow
        )
        if shadow:
            counters.removed += 1
            logger.debug(
                "Skipped repeated shot at %s (near-identical to an earlier retained frame)",
                format_timestamp(timestamp),
            )

    try:
        for timestamp, frame in frames:
            try:
                signature = analyze_frame(frame)
            except (ValueError, cv2.error) as exc:
                errors.append(f"Could not analyze a frame at {format_timestamp(timestamp)}: {exc}")
                logger.exception("Frame analysis failed")
                break

            scene_cut = (
                previous_signature is not None
                and is_likely_scene_cut(previous_signature, signature)
            )
            try:
                if pending is None:
                    start_group(frame, signature, timestamp)
                elif scene_cut:
                    _save_pending(pending, writer, counters, records, recent)
                    pending = None
                    start_group(frame, signature, timestamp)
                else:
                    result = compare_signatures(pending.signature, signature)
                    redundant = (
                        result.score * 100.0 >= threshold
                        and result.changed_fraction < motion
                    )
                    if redundant:
                        counters.removed += 1
                        if (
                            not pending.shadow
                            and not result.is_exact_duplicate
                            and signature.sharpness > pending.signature.sharpness * 1.25
                        ):
                            pending = _PendingRepresentative(
                                frame=frame,
                                signature=signature,
                                timestamp=timestamp,
                                shadow=False,
                            )
                            logger.debug(
                                "Replaced buffered frame at %s with a sharper frame",
                                format_timestamp(timestamp),
                            )
                    else:
                        _save_pending(pending, writer, counters, records, recent)
                        pending = None
                        start_group(frame, signature, timestamp)
            except OSError as exc:
                errors.append(str(exc))
                logger.exception("Image write failed")
                pending = None
                break

            previous_signature = signature
            reporter.report(counters, timestamp, "Filtering similar frames")
    except ExtractionCancelled:
        # Keep the buffered representative so a cancelled job still contains usable frames.
        if pending is not None and not pending.shadow:
            try:
                _save_pending(pending, writer, counters, records, recent)
            except OSError as exc:
                errors.append(str(exc))
        raise

    if pending is not None and not pending.shadow:
        try:
            _save_pending(pending, writer, counters, records, recent)
        except OSError as exc:
            errors.append(str(exc))
            logger.exception("Image write failed while finalising the last frame")
        pending = None


def _process_without_filtering(
    frames: Iterator[tuple[float, np.ndarray]],
    writer: FrameWriter,
    counters: _Counters,
    reporter: _ProgressReporter,
    records: list[FrameRecord],
    errors: list[str],
) -> None:
    for timestamp, frame in frames:
        try:
            record = writer.save(frame, timestamp)
        except OSError as exc:
            errors.append(str(exc))
            logger.exception("Image write failed")
            break
        records.append(record)
        counters.retained += 1
        reporter.report(counters, timestamp, "Saving frames")


def run_extraction(
    settings: ExtractionSettings,
    control: ExtractionControl | None = None,
    progress_callback: Callable[[ExtractionProgress], None] | None = None,
) -> ExtractionResult:
    """Extract frames according to settings. Raises ValidationError/VideoError before work starts."""
    validate_settings(settings)
    info = probe_video(settings.video_path)
    _check_free_space(settings.output_root)
    control = control or ExtractionControl()

    start = settings.start_time
    if info.duration is not None and start >= info.duration:
        raise ValidationError(
            f"The start time ({format_timestamp(start)}) is at or after the end of the "
            f"video ({format_timestamp(info.duration)})."
        )
    end_time = settings.end_time
    if end_time is not None and info.duration is not None and end_time > info.duration:
        end_time = info.duration
        logger.info("End time clamped to the video duration")

    stem = safe_stem(settings.video_path.name)
    job_dir = _create_job_dir(settings.output_root, stem)
    log_handler = logging.FileHandler(job_dir / "extraction.log", encoding="utf-8")
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(log_handler)
    logger.setLevel(logging.INFO)

    started_at = time.monotonic()
    counters = _Counters()
    records: list[FrameRecord] = []
    errors: list[str] = []
    warnings: list[str] = []
    cancelled = False
    reporter = _ProgressReporter(
        progress_callback,
        started_at,
        start,
        end_time if end_time is not None else info.duration,
    )
    try:
        writer = FrameWriter(job_dir, stem, settings)
    except OSError as exc:
        logger.exception("Could not prepare output folders")
        logger.removeHandler(log_handler)
        log_handler.close()
        raise VideoError(f"Could not create the output folders: {exc}") from exc
    capture: cv2.VideoCapture | None = None

    logger.info("Video: %s (%s, %s, %s)", settings.video_path, info.resolution_label, info.fps_label, info.duration)
    logger.info("Job folder: %s", job_dir)
    logger.info(
        "Mode=%s start=%.3f end=%s interval=%.3f frame_interval=%d filtering=%s threshold=%.1f format=%s resize=%s",
        settings.mode,
        start,
        end_time,
        settings.interval_seconds,
        settings.frame_interval,
        settings.similarity.enabled or settings.mode == MODE_SCENE,
        settings.similarity.effective_threshold,
        settings.image_format,
        settings.resize.mode,
    )

    try:
        capture = _open_capture(settings.video_path)
        reporter.report(counters, start, "Starting extraction", force=True)
        frames = _iter_candidates(
            capture, info, settings, control, reporter, counters, end_time
        )
        filtering_enabled = settings.similarity.enabled or settings.mode == MODE_SCENE
        if filtering_enabled:
            _process_with_filtering(
                frames, settings, writer, counters, reporter, records, errors
            )
        else:
            _process_without_filtering(
                frames, writer, counters, reporter, records, errors
            )
    except ExtractionCancelled:
        cancelled = True
        logger.info("Extraction cancelled by user")
    except (VideoError, OSError) as exc:
        errors.append(str(exc))
        logger.exception("Extraction stopped")
    except Exception as exc:  # Keep partial output usable and show the problem in the GUI.
        errors.append(f"Unexpected processing error: {exc}")
        logger.exception("Unexpected extraction error")
    finally:
        if capture is not None:
            capture.release()

    warnings.extend(writer.warnings)
    if counters.candidates == 0 and not errors and not cancelled:
        errors.append("No frames could be decoded in the selected time range.")

    if records and settings.contact_sheet.enabled:
        reporter.report(counters, end_time or start, "Generating contact sheets", force=True)
        try:
            contact_sheet_paths = create_contact_sheets(
                job_dir=job_dir,
                video_name=settings.video_path.name,
                records=records,
                settings=settings.contact_sheet,
            )
            result_sheets = contact_sheet_paths
        except Exception as exc:
            result_sheets = []
            errors.append(f"Contact sheet generation failed: {exc}")
            logger.exception("Contact sheet generation failed")
    else:
        result_sheets = []

    metadata_path: Path | None = None
    if settings.write_metadata:
        try:
            metadata_path = write_metadata(
                job_dir=job_dir,
                settings=settings,
                info=info,
                records=records,
                candidate_frames=counters.candidates,
                retained_frames=counters.retained,
                removed_frames=counters.removed,
                errors=errors,
                warnings=warnings,
                contact_sheets=result_sheets,
                cancelled=cancelled,
                elapsed_seconds=time.monotonic() - started_at,
                effective_start=start,
                effective_end=end_time,
            )
        except Exception as exc:
            errors.append(f"Could not write metadata.json: {exc}")
            logger.exception("Metadata write failed")

    elapsed = time.monotonic() - started_at
    logger.info(
        "Finished: candidates=%d retained=%d removed=%d cancelled=%s elapsed=%.1fs",
        counters.candidates,
        counters.retained,
        counters.removed,
        cancelled,
        elapsed,
    )
    logger.removeHandler(log_handler)
    log_handler.close()

    return ExtractionResult(
        job_dir=job_dir,
        video_info=info,
        records=records,
        candidate_frames=counters.candidates,
        retained_frames=counters.retained,
        removed_frames=counters.removed,
        contact_sheets=result_sheets,
        metadata_path=metadata_path,
        errors=errors,
        warnings=warnings,
        cancelled=cancelled,
        elapsed_seconds=elapsed,
    )
