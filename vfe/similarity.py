"""Perceptual signatures, similarity scoring, motion and scene-change detection.

The filter is deliberately conservative: a frame is only treated as redundant
when the perceptual score is high AND the motion guard finds no meaningful
change. Each new candidate is compared with the buffered representative of the
current visual group, not merely with the previous frame, so gradual drift
cannot make an entire scene collapse into one image.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import imagehash
import numpy as np
from PIL import Image

ANALYSIS_WIDTH = 320
GRAY_SIZE = (160, 90)
HASH_SIZE = 8
CHANGE_PIXEL_THRESHOLD = 12
SCENE_CUT_FRACTION = 0.32


@dataclass(frozen=True)
class FrameSignature:
    """Compact representation of a frame. Small enough to keep many in memory."""

    phash: int
    dhash: int
    gray: np.ndarray
    color_hist: np.ndarray
    sharpness: float


@dataclass(frozen=True)
class SimilarityResult:
    score: float
    phash_distance: int
    dhash_distance: int
    ssim: float
    color: float
    changed_fraction: float

    @property
    def is_exact_duplicate(self) -> bool:
        return (
            self.phash_distance == 0
            and self.dhash_distance == 0
            and self.ssim >= 0.9995
            and self.color >= 0.9995
        )


def _bits_to_int(bits: np.ndarray) -> int:
    packed = np.packbits(bits.astype(np.uint8)).tobytes()
    return int.from_bytes(packed, "big")


def _ssim(first: np.ndarray, second: np.ndarray) -> float:
    """Mean SSIM on single-channel images without scikit-image."""
    a = first.astype(np.float64)
    b = second.astype(np.float64)
    kernel = (11, 11)
    sigma = 1.5
    mu_a = cv2.GaussianBlur(a, kernel, sigma)
    mu_b = cv2.GaussianBlur(b, kernel, sigma)
    mu_a_sq = mu_a * mu_a
    mu_b_sq = mu_b * mu_b
    mu_ab = mu_a * mu_b
    sigma_a_sq = cv2.GaussianBlur(a * a, kernel, sigma) - mu_a_sq
    sigma_b_sq = cv2.GaussianBlur(b * b, kernel, sigma) - mu_b_sq
    sigma_ab = cv2.GaussianBlur(a * b, kernel, sigma) - mu_ab
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    ssim_map = ((2 * mu_ab + c1) * (2 * sigma_ab + c2)) / (
        (mu_a_sq + mu_b_sq + c1) * (sigma_a_sq + sigma_b_sq + c2)
    )
    return float(np.clip(ssim_map.mean(), 0.0, 1.0))


def _changed_fraction(first: np.ndarray, second: np.ndarray) -> float:
    diff = cv2.absdiff(first, second)
    return float(np.count_nonzero(diff > CHANGE_PIXEL_THRESHOLD) / diff.size)


def analyze_frame(frame: np.ndarray) -> FrameSignature:
    """Build a compact perceptual signature from a BGR OpenCV frame."""
    height, width = frame.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("The frame is empty.")
    scale = min(1.0, ANALYSIS_WIDTH / width)
    if scale < 1.0:
        small = cv2.resize(
            frame,
            (max(8, round(width * scale)), max(8, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = frame

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray_norm = cv2.resize(gray, GRAY_SIZE, interpolation=cv2.INTER_AREA)
    gray_blur = cv2.GaussianBlur(gray_norm, (5, 5), 0)
    sharpness = float(cv2.Laplacian(gray_blur, cv2.CV_64F).var())

    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    phash = _bits_to_int(imagehash.phash(image, hash_size=HASH_SIZE).hash)
    dhash = _bits_to_int(imagehash.dhash(image, hash_size=HASH_SIZE).hash)

    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    color_hist = cv2.calcHist([hsv], [0, 1], None, [24, 24], [0, 180, 0, 256])
    cv2.normalize(color_hist, color_hist, alpha=1.0, beta=0.0, norm_type=cv2.NORM_L1)

    return FrameSignature(
        phash=phash,
        dhash=dhash,
        gray=gray_blur,
        color_hist=color_hist,
        sharpness=sharpness,
    )


def compare_signatures(first: FrameSignature, second: FrameSignature) -> SimilarityResult:
    """Compare two signatures and return a 0..1 perceptual similarity score."""
    phash_distance = (first.phash ^ second.phash).bit_count()
    dhash_distance = (first.dhash ^ second.dhash).bit_count()
    hash_similarity = 1.0 - (0.6 * phash_distance + 0.4 * dhash_distance) / 64.0
    ssim = _ssim(first.gray, second.gray)
    color = float(cv2.compareHist(first.color_hist, second.color_hist, cv2.HISTCMP_CORREL))
    color = float(np.clip(color, 0.0, 1.0))
    score = 0.45 * ssim + 0.30 * hash_similarity + 0.25 * color
    return SimilarityResult(
        score=float(np.clip(score, 0.0, 1.0)),
        phash_distance=phash_distance,
        dhash_distance=dhash_distance,
        ssim=ssim,
        color=color,
        changed_fraction=_changed_fraction(first.gray, second.gray),
    )


def is_likely_scene_cut(previous: FrameSignature, current: FrameSignature) -> bool:
    """Detect a hard visual cut between consecutive analyzed frames."""
    return _changed_fraction(previous.gray, current.gray) >= SCENE_CUT_FRACTION
