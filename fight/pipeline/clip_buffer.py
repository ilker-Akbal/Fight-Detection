from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2


def _write_with_opencv(frames_bgr, out_path: str, fps: float, fourcc_codes: list[str]) -> bool:
    if not frames_bgr:
        return False

    h, w = frames_bgr[0].shape[:2]
    out_path = str(out_path)

    for code in fourcc_codes:
        fourcc = cv2.VideoWriter_fourcc(*code)
        writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))
        if not writer.isOpened():
            try:
                writer.release()
            except Exception:
                pass
            continue

        try:
            for f in frames_bgr:
                if f is None:
                    continue
                if f.shape[:2] != (h, w):
                    f = cv2.resize(f, (w, h))
                writer.write(f)
        finally:
            writer.release()

        if Path(out_path).exists() and Path(out_path).stat().st_size > 0:
            return True

    return False


def _ffmpeg_path() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    return ffmpeg


def _transcode_to_h264_ffmpeg(frames_bgr, out_path: str, fps: float) -> bool:
    ffmpeg = _ffmpeg_path()
    if not ffmpeg or not frames_bgr:
        return False

    h, w = frames_bgr[0].shape[:2]

    with tempfile.TemporaryDirectory() as td:
        raw_avi = str(Path(td) / "temp_input.avi")

        ok = _write_with_opencv(
            frames_bgr=frames_bgr,
            out_path=raw_avi,
            fps=fps,
            fourcc_codes=["MJPG", "XVID"],
        )
        if not ok:
            return False

        cmd = [
            ffmpeg,
            "-y",
            "-i",
            raw_avi,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(out_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                return False
        except Exception:
            return False

    return Path(out_path).exists() and Path(out_path).stat().st_size > 0


def save_clip_mp4(frames_bgr, out_path: str, fps: float = 16.0):
    if not frames_bgr:
        return

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # 1) En iyi tarayıcı uyumu için ffmpeg + h264 dene
    if _transcode_to_h264_ffmpeg(frames_bgr, out_path, fps):
        return

    # 2) ffmpeg yoksa OpenCV ile H264 benzeri codec dene
    if _write_with_opencv(
        frames_bgr=frames_bgr,
        out_path=out_path,
        fps=fps,
        fourcc_codes=["avc1", "H264", "X264", "mp4v"],
    ):
        return

    raise RuntimeError(f"Clip yazılamadı: {out_path}")