from __future__ import annotations

from typing import List

import cv2
import numpy as np
import torch


KINETICS_MEAN = np.array([0.45, 0.45, 0.45], dtype=np.float32)
KINETICS_STD = np.array([0.225, 0.225, 0.225], dtype=np.float32)


def _linspace_indices(n: int, k: int) -> list[int]:
    if n <= 0:
        return []
    if k <= 1:
        return [max(0, n // 2)]
    return np.linspace(0, n - 1, k).astype(np.int64).tolist()


def sample_x3d_frames(frames_bgr: List[np.ndarray], num_frames: int = 16) -> List[np.ndarray]:
    frames = list(frames_bgr)
    if not frames:
        return []

    if len(frames) < num_frames:
        last = frames[-1]
        frames = frames + [last] * (num_frames - len(frames))

    ids = _linspace_indices(len(frames), int(num_frames))
    return [frames[int(i)] for i in ids]


def _resize_short_side_rgb(rgb: np.ndarray, short_side: int = 256) -> np.ndarray:
    h, w = rgb.shape[:2]
    short = max(1, min(h, w))
    scale = float(short_side) / float(short)

    nh = max(1, int(round(h * scale)))
    nw = max(1, int(round(w * scale)))

    return cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)


def _center_crop_rgb(rgb: np.ndarray, size: int = 224) -> np.ndarray:
    h, w = rgb.shape[:2]

    if h < size or w < size:
        scale = float(size) / float(max(1, min(h, w)))
        nh = max(size, int(round(h * scale)))
        nw = max(size, int(round(w * scale)))
        rgb = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        h, w = rgb.shape[:2]

    top = max(0, (h - size) // 2)
    left = max(0, (w - size) // 2)

    return rgb[top:top + size, left:left + size]


def preprocess_clip(
    frames_bgr: list[np.ndarray],
    size: int = 224,
    num_frames: int = 16,
    mean: np.ndarray | None = None,
    std: np.ndarray | None = None,
) -> torch.Tensor:
    """
    X3D-M offline/batch inference preprocess.

    Eğitim koduyla uyumlu:
    - BGR -> RGB
    - 64 context içinden 16 frame sample edilmiş olarak gelir veya burada 16'ya sample edilir
    - short side 256 resize
    - center crop 224
    - mean/std = [0.45]/[0.225]
    - çıktı: C,T,H,W
    """
    if mean is None:
        mean = KINETICS_MEAN
    if std is None:
        std = KINETICS_STD

    sampled = sample_x3d_frames(frames_bgr, num_frames=int(num_frames))
    if not sampled:
        raise RuntimeError("preprocess_clip: frame listesi boş")

    processed = []

    for f in sampled:
        rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
        rgb = _resize_short_side_rgb(rgb, short_side=256)
        rgb = _center_crop_rgb(rgb, size=int(size))

        x = rgb.astype(np.float32) / 255.0
        x = (x - mean) / std
        processed.append(x)

    clip = np.stack(processed, axis=0)  # T,H,W,C
    clip = torch.from_numpy(clip).permute(3, 0, 1, 2).contiguous()  # C,T,H,W
    return clip