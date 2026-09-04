from __future__ import annotations

from pathlib import Path

from fight.pipeline.media_encoding import save_frames_browser_mp4


def save_clip_mp4(frames_bgr, out_path: str, fps: float = 16.0):
    if not frames_bgr:
        return

    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if save_frames_browser_mp4(frames_bgr, out_path, fps):
        return
    raise RuntimeError(f"Clip could not be written: {out_path}")
