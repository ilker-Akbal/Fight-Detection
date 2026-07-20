from __future__ import annotations

import cv2
import numpy as np

from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class VideoInfo:
    fps: float
    total_frames: int
    w: int
    h: int


def read_video_info(video_path: str) -> VideoInfo:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()

    return VideoInfo(fps=float(fps), total_frames=total, w=w, h=h)


def sample_indices(total_frames: int, step: int) -> List[int]:
    if total_frames <= 0:
        return []
    step = max(1, int(step))
    return list(range(0, total_frames, step))


def extract_frames_by_indices(video_path: str, indices: List[int]) -> List:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")

    frames = []
    idx_set = set(int(i) for i in indices)
    i = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if i in idx_set:
            frames.append(frame)

        i += 1

    cap.release()
    return frames


def make_context_windows(
    frames: List,
    context_frames: int = 64,
    stride: int | None = None,
) -> List[List]:
    """
    X3D-M için offline event videosundan context pencereleri üretir.

    Eğitim mantığı:
    - context_frames=64
    - bu context içinden model için 16 frame sample edilir.
    """
    if not frames:
        return []

    context_frames = max(1, int(context_frames))

    if stride is None:
        stride = context_frames

    stride = max(1, int(stride))

    n = len(frames)

    if n <= context_frames:
        out = list(frames)
        if len(out) < context_frames:
            out = out + [out[-1]] * (context_frames - len(out))
        return [out]

    clips = []

    for start in range(0, n - context_frames + 1, stride):
        clips.append(frames[start:start + context_frames])

    if (n - context_frames) % stride != 0:
        clips.append(frames[-context_frames:])

    return clips


def make_clips(frames: List, clip_len: int = 32, stride: int | None = None) -> List[List]:
    """
    Eski API uyumluluğu için bırakıldı.
    Yeni X3D-M yolunda make_context_windows kullanılmalı.
    """
    if stride is None:
        stride = clip_len

    clips = []
    n = len(frames)

    if n == 0:
        return clips

    if n < clip_len:
        last = frames[-1]
        frames = frames + [last] * (clip_len - n)
        clips.append(frames[:clip_len])
        return clips

    for start in range(0, n - clip_len + 1, stride):
        clips.append(frames[start:start + clip_len])

    if (n - clip_len) % stride != 0:
        clips.append(frames[-clip_len:])

    return clips


def load_event_clips(
    video_path: str,
    clip_len: int = 16,
    fps_sample: int = 16,
    context_frames: int = 64,
    context_stride: int | None = None,
) -> Tuple[VideoInfo, List[List]]:
    """
    Offline/batch inference için event video okur.

    Dönüş:
        info, context_windows

    Her context window daha sonra preprocess_clip içinde 16 frame'e sample edilir.
    """
    info = read_video_info(video_path)

    fps = info.fps if info.fps and info.fps > 1e-3 else 30.0
    step = max(1, int(round(fps / float(max(1, fps_sample)))))

    idx = sample_indices(info.total_frames, step=step)
    frames = extract_frames_by_indices(video_path, idx)

    # context_frames yoksa eski davranışa yakın kalsın.
    if context_frames is None or int(context_frames) <= 0:
        clips = make_clips(frames, clip_len=int(clip_len), stride=int(clip_len))
    else:
        clips = make_context_windows(
            frames,
            context_frames=int(context_frames),
            stride=context_stride,
        )

    return info, clips