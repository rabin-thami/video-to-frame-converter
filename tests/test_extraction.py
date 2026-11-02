"""Automated tests for the extraction core. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from vfe.config import (
    ContactSheetSettings,
    ExtractionSettings,
    ResizeSettings,
    SimilaritySettings,
)
from vfe.errors import ValidationError, VideoError
from vfe.extractor import ExtractionControl, run_extraction
from vfe.probe import probe_video
from vfe.similarity import analyze_frame, compare_signatures
from vfe.utils import format_timestamp, parse_time


def make_test_video(path: Path, fps: int = 30) -> None:
    """Create a 7-second video: static shots, gradual motion, and a repeated shot."""
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 360)
    )
    if not writer.isOpened():
        raise RuntimeError("Could not create test video")
    rng = np.random.default_rng(3)
    texture = rng.integers(60, 200, (360, 640, 3), dtype=np.uint8)

    # Scene 1: static red (0.0 - 1.0 s)
    for _ in range(30):
        frame = texture.copy()
        frame[:, :, 0] = np.clip(30 + frame[:, :, 0] // 3, 0, 255)
        writer.write(frame)
    # Scene 2: static blue (1.0 - 2.0 s)
    for _ in range(30):
        frame = texture.copy()
        frame[:, :, 1] = np.clip(30 + frame[:, :, 1] // 3, 0, 255)
        writer.write(frame)
    # Scene 3: dark shot with a white box moving slowly (2.0 - 5.0 s)
    for index in range(90):
        frame = np.full((360, 640, 3), 40, dtype=np.uint8)
        x = 50 + index * 5
        cv2.rectangle(frame, (x, 140), (x + 80, 220), (245, 245, 245), -1)
        writer.write(frame)
    # Scene 4: static green (5.0 - 6.0 s)
    for _ in range(30):
        frame = texture.copy()
        frame[:, :, 2] = np.clip(30 + frame[:, :, 2] // 3, 0, 255)
        writer.write(frame)
    # Scene 5: the same static red shot as scene 1 (6.0 - 7.0 s) - should not be duplicated.
    for _ in range(30):
        frame = texture.copy()
        frame[:, :, 0] = np.clip(30 + frame[:, :, 0] // 3, 0, 255)
        writer.write(frame)
    writer.release()


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.video = cls.root / "synthetic_scene_test.avi"
        make_test_video(cls.video)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def settings(self, name: str, **overrides) -> ExtractionSettings:
        defaults = {
            "video_path": self.video,
            "output_root": self.root / name,
            "mode": "every_frame",
            "similarity": SimilaritySettings(enabled=False),
            "resize": ResizeSettings(mode="original"),
            "contact_sheet": ContactSheetSettings(enabled=False),
        }
        defaults.update(overrides)
        return ExtractionSettings(**defaults)

    def test_probe_metadata(self) -> None:
        info = probe_video(self.video)
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertAlmostEqual(info.fps or 0, 30.0, places=2)
        self.assertEqual(info.frame_count, 210)
        self.assertAlmostEqual(info.duration or 0, 7.0, places=1)

    def test_every_frame_without_filtering(self) -> None:
        result = run_extraction(self.settings("all_frames"))
        self.assertFalse(result.errors)
        self.assertEqual(result.candidate_frames, 210)
        self.assertEqual(result.retained_frames, 210)
        self.assertEqual(result.removed_frames, 0)
        self.assertEqual(len(list((result.job_dir / "frames").glob("*.png"))), 210)
        self.assertEqual(result.records[0].path.name, "synthetic_scene_test_frame_000001.png")
        self.assertTrue(result.metadata_path and result.metadata_path.exists())
        payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["results"]["retained_images"], 210)
        self.assertEqual(len(payload["frames"]), 210)

    def test_similarity_filtering_preserves_scene_changes_and_motion(self) -> None:
        result = run_extraction(
            self.settings(
                "filtered",
                similarity=SimilaritySettings(enabled=True, threshold=90.0),
                contact_sheet=ContactSheetSettings(
                    enabled=True, columns=3, max_thumbnails=5
                ),
            )
        )
        self.assertFalse(result.errors, result.errors)
        self.assertEqual(result.candidate_frames, 210)
        self.assertLess(result.retained_frames, 40)
        self.assertGreaterEqual(result.retained_frames, 5)
        self.assertEqual(
            result.candidate_frames,
            result.retained_frames + result.removed_frames,
        )
        timestamps = [record.timestamp for record in result.records]
        # Hard scene boundaries are kept.
        for expected in (1.0, 2.0, 5.0):
            self.assertTrue(
                any(abs(timestamp - expected) < 0.08 for timestamp in timestamps),
                f"missing representative near {expected}s",
            )
        # Motion inside a static-background shot is not collapsed to one image.
        motion_frames = [t for t in timestamps if 2.2 <= t <= 4.9]
        self.assertGreaterEqual(len(motion_frames), 3)
        # A repeated identical shot is not saved twice.
        self.assertFalse(
            [t for t in timestamps if t >= 5.99],
            "the repeated red shot should be suppressed as a duplicate",
        )
        self.assertGreater(len(result.records), 0)
        self.assertEqual(
            len(result.contact_sheets), math.ceil(len(result.records) / 5)
        )
        for sheet in result.contact_sheets:
            self.assertTrue(sheet.exists())
        payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["similarity_filtering"]["enabled"])
        self.assertEqual(payload["results"]["candidate_frames"], 210)
        self.assertEqual(payload["results"]["retained_images"], len(result.records))

    def test_preserve_more_keeps_at_least_as_many(self) -> None:
        normal = run_extraction(
            self.settings(
                "normal_filter",
                similarity=SimilaritySettings(enabled=True, threshold=90.0),
            )
        )
        preserved = run_extraction(
            self.settings(
                "preserve_filter",
                similarity=SimilaritySettings(
                    enabled=True, threshold=90.0, preserve_more=True
                ),
            )
        )
        self.assertGreaterEqual(
            preserved.retained_frames, normal.retained_frames
        )

    def test_time_interval_and_selected_range(self) -> None:
        result = run_extraction(
            self.settings(
                "time_range",
                mode="time_interval",
                interval_seconds=0.5,
                start_time=1.0,
                end_time=3.0,
            )
        )
        self.assertFalse(result.errors)
        self.assertEqual(result.candidate_frames, 5)
        self.assertEqual(result.retained_frames, 5)
        for record in result.records:
            self.assertGreaterEqual(record.timestamp, 0.99)
            self.assertLessEqual(record.timestamp, 3.02)

    def test_frame_interval(self) -> None:
        result = run_extraction(
            self.settings(
                "frame_interval", mode="frame_interval", frame_interval=30
            )
        )
        self.assertFalse(result.errors)
        self.assertEqual(result.candidate_frames, 7)
        self.assertEqual(result.retained_frames, 7)
        for expected, record in enumerate(result.records):
            self.assertAlmostEqual(record.timestamp, expected, delta=0.05)

    def test_scene_mode_forces_filtering(self) -> None:
        result = run_extraction(
            self.settings(
                "scene_mode",
                mode="scene",
                similarity=SimilaritySettings(enabled=False, threshold=90.0),
            )
        )
        self.assertFalse(result.errors)
        self.assertEqual(result.candidate_frames, 210)
        self.assertLess(result.retained_frames, 40)
        self.assertGreaterEqual(result.retained_frames, 5)

    def test_resize_and_original_copies(self) -> None:
        result = run_extraction(
            self.settings(
                "resized",
                mode="time_interval",
                interval_seconds=1.0,
                resize=ResizeSettings(
                    mode="max_width", width=320, save_originals=True
                ),
            )
        )
        self.assertFalse(result.errors)
        self.assertTrue(result.records)
        for record in result.records:
            with Image.open(record.path) as image:
                self.assertLessEqual(image.width, 320)
                self.assertGreaterEqual(image.width, 300)
            self.assertIsNotNone(record.original_path)
            assert record.original_path is not None
            with Image.open(record.original_path) as image:
                self.assertEqual(image.width, 640)
        payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertIn("original_filename", payload["frames"][0])

    def test_cancellation_keeps_partial_output(self) -> None:
        control = ExtractionControl()

        def cancel_after_ten(progress) -> None:
            if progress.candidates >= 10:
                control.cancel()

        result = run_extraction(
            self.settings("cancelled"),
            control=control,
            progress_callback=cancel_after_ten,
        )
        self.assertTrue(result.cancelled)
        self.assertGreater(result.retained_frames, 0)
        self.assertEqual(len(result.records), result.retained_frames)
        self.assertTrue(result.metadata_path and result.metadata_path.exists())
        self.assertFalse(list(result.job_dir.rglob("*.part")))
        payload = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["cancelled"])

    def test_output_folder_conflicts_create_unique_jobs(self) -> None:
        common = {"mode": "frame_interval", "frame_interval": 30}
        first = run_extraction(self.settings("conflict", **common))
        second = run_extraction(self.settings("conflict", **common))
        self.assertNotEqual(first.job_dir, second.job_dir)
        self.assertTrue(first.job_dir.exists() and second.job_dir.exists())

    def test_unreadable_video_is_rejected(self) -> None:
        broken = self.root / "broken.mp4"
        broken.write_bytes(b"this is not a video")
        settings = self.settings("broken", video_path=broken)
        with self.assertRaises(VideoError):
            run_extraction(settings)

    def test_missing_video_is_rejected(self) -> None:
        settings = self.settings("missing", video_path=self.root / "nope.mp4")
        with self.assertRaises(ValidationError):
            run_extraction(settings)


class SimilarityTests(unittest.TestCase):
    def test_identical_frames_are_exact_duplicates(self) -> None:
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        cv2.rectangle(frame, (40, 40), (160, 120), (0, 255, 0), -1)
        first = analyze_frame(frame)
        second = analyze_frame(frame.copy())
        result = compare_signatures(first, second)
        self.assertGreater(result.score, 0.999)
        self.assertTrue(result.is_exact_duplicate)

    def test_different_frames_score_low(self) -> None:
        first_frame = np.full((180, 320, 3), (30, 30, 200), dtype=np.uint8)
        second_frame = np.full((180, 320, 3), (200, 30, 30), dtype=np.uint8)
        cv2.circle(second_frame, (240, 90), 50, (10, 220, 10), -1)
        first = analyze_frame(first_frame)
        second = analyze_frame(second_frame)
        result = compare_signatures(first, second)
        self.assertLess(result.score, 0.9)
        self.assertFalse(result.is_exact_duplicate)

    def test_small_motion_is_detected(self) -> None:
        first_frame = np.full((180, 320, 3), 40, dtype=np.uint8)
        second_frame = first_frame.copy()
        cv2.rectangle(first_frame, (20, 60), (70, 120), (250, 250, 250), -1)
        cv2.rectangle(second_frame, (60, 60), (110, 120), (250, 250, 250), -1)
        result = compare_signatures(
            analyze_frame(first_frame), analyze_frame(second_frame)
        )
        self.assertGreater(result.changed_fraction, 0.01)


class TimeFormattingTests(unittest.TestCase):
    def test_timestamp_rounding_carries_into_next_minute(self) -> None:
        self.assertEqual(format_timestamp(59.9996), "00:01:00.000")
        self.assertEqual(format_timestamp(3599.6, milliseconds=False), "01:00:00")

    def test_parse_time_accepts_seconds_and_clock_format(self) -> None:
        self.assertAlmostEqual(parse_time("12.5"), 12.5)
        self.assertAlmostEqual(parse_time("00:01:02.5"), 62.5)
        self.assertAlmostEqual(parse_time("1:02"), 62.0)


if __name__ == "__main__":
    unittest.main()
