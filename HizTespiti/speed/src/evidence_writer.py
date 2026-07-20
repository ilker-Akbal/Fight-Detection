from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import shutil
import subprocess
import time

import cv2

from HizTespiti.yolo.src.simple_tracker import Track

from .speed_config import EvidenceConfig


@dataclass
class SpeedViolationEvent:
    camera_id: str
    frame_idx: int
    timestamp_sec: float
    track_id: int
    vehicle_class: str
    speed_kmh: float
    speed_limit_kmh: float
    tolerance_kmh: float
    threshold_kmh: float
    box_xyxy: list[int]
    snapshot_path: str | None
    clip_path: str | None
    created_at: float


class FrameBuffer:
    def __init__(self, max_frames: int):
        self.frames = deque(maxlen=max_frames)

    def add(self, frame_idx: int, frame):
        self.frames.append((frame_idx, frame.copy()))

    def get_from(self, start_frame: int):
        return [(idx, fr) for idx, fr in self.frames if idx >= start_frame]


class EvidenceWriter:
    def __init__(
        self,
        output_dir: str | Path,
        camera_id: str,
        cfg: EvidenceConfig,
        fps: float,
    ):
        self.output_dir = Path(output_dir)
        self.camera_id = camera_id
        self.cfg = cfg
        self.fps = max(float(fps), 1.0)

        self.event_dir = self.output_dir / "events"
        self.snapshot_dir = self.output_dir / "snapshots"
        self.clip_dir = self.output_dir / "clips"

        self.event_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)

        self.events_path = self.event_dir / f"{camera_id}_speed_violations.jsonl"

    def _stamp(self) -> str:
        return time.strftime("%Y%m%d_%H%M%S")


    def _encode_mp4v_temp(self, path: Path, frames: list, fps: float, size: tuple[int, int]) -> bool:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, fps, size)

        if not writer.isOpened():
            return False

        try:
            for frame in frames:
                writer.write(frame)
        finally:
            writer.release()

        return path.exists() and path.stat().st_size > 0

    def _convert_to_browser_mp4(self, src_path: Path, dst_path: Path) -> bool:
        ffmpeg = shutil.which("ffmpeg")

        if not ffmpeg:
            return False

        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src_path),
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dst_path),
        ]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return False

        return dst_path.exists() and dst_path.stat().st_size > 0

    def _write_clip(self, clip_path: Path, frames_with_idx: list[tuple[int, object]], frame_idx: int, frame_vis) -> None:
        if not frames_with_idx:
            return

        frames = [fr for _, fr in frames_with_idx]

        # Eğer buffer güncel frame'i henüz içermiyorsa son kareyi de ekle.
        # İşlenmiş frame buffer kullanıldığında duplicate frame oluşmasın.
        if frames_with_idx[-1][0] != frame_idx:
            frames.append(frame_vis)

        h, w = frames[0].shape[:2]
        size = (int(w), int(h))

        raw_path = clip_path.with_suffix(".raw.mp4")

        try:
            if raw_path.exists():
                raw_path.unlink()
            if clip_path.exists():
                clip_path.unlink()
        except Exception:
            pass

        if not self._encode_mp4v_temp(raw_path, frames, self.fps, size):
            return

        # Tarayıcılar mp4v codec'i çoğu zaman oynatmaz. Mümkünse H.264/yuv420p'e çevir.
        if self._convert_to_browser_mp4(raw_path, clip_path):
            try:
                raw_path.unlink()
            except Exception:
                pass
            return

        # ffmpeg yoksa en azından eski davranış bozulmasın.
        try:
            raw_path.replace(clip_path)
        except Exception:
            pass

    def save_event(
        self,
        frame_idx: int,
        timestamp_sec: float,
        frame_vis,
        track: Track,
        speed_kmh: float,
        speed_limit_kmh: float,
        tolerance_kmh: float,
        threshold_kmh: float,
        frame_buffer: FrameBuffer,
    ) -> SpeedViolationEvent:
        stamp = self._stamp()
        base_name = f"{self.camera_id}_track{track.track_id}_{int(speed_kmh)}kmh_{stamp}_f{frame_idx}"

        snapshot_path = None
        clip_path = None

        if self.cfg.save_snapshot:
            snapshot_path = str(self.snapshot_dir / f"{base_name}.jpg")
            cv2.imwrite(
                snapshot_path,
                frame_vis,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(self.cfg.jpeg_quality)],
            )

        if self.cfg.save_clip:
            start_frame = int(frame_idx - self.cfg.clip_pre_sec * self.fps)
            frames = frame_buffer.get_from(start_frame)

            if frames:
                clip_file = self.clip_dir / f"{base_name}.mp4"
                self._write_clip(clip_file, frames, frame_idx, frame_vis)

                if clip_file.exists() and clip_file.stat().st_size > 0:
                    clip_path = str(clip_file)

        x1, y1, x2, y2 = [int(v) for v in track.box]

        event = SpeedViolationEvent(
            camera_id=self.camera_id,
            frame_idx=frame_idx,
            timestamp_sec=timestamp_sec,
            track_id=track.track_id,
            vehicle_class=track.cls_name,
            speed_kmh=float(speed_kmh),
            speed_limit_kmh=float(speed_limit_kmh),
            tolerance_kmh=float(tolerance_kmh),
            threshold_kmh=float(threshold_kmh),
            box_xyxy=[x1, y1, x2, y2],
            snapshot_path=snapshot_path,
            clip_path=clip_path,
            created_at=time.time(),
        )

        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

        return event