from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def ts_to_str(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_file_source(source: str) -> bool:
    s = str(source).strip()
    if not s:
        return False
    if s.isdigit():
        return False
    low = s.lower()
    if low.startswith(("rtsp://", "rtmp://", "http://", "https://", "udp://", "tcp://")):
        return False
    return Path(s).exists()


def configure_process_runtime(cv2_threads: int = 1, enable_cuda_tuning: bool = False) -> None:
    cv2_threads = max(1, int(cv2_threads))

    try:
        cv2.setNumThreads(cv2_threads)
    except Exception:
        pass

    os.environ.setdefault("OMP_NUM_THREADS", str(cv2_threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(cv2_threads))
    os.environ.setdefault("NUMEXPR_NUM_THREADS", str(cv2_threads))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", str(cv2_threads))

    if enable_cuda_tuning:
        try:
            import torch

            if torch.cuda.is_available():
                try:
                    torch.backends.cudnn.benchmark = True
                except Exception:
                    pass
                try:
                    torch.backends.cuda.matmul.allow_tf32 = True
                    torch.backends.cudnn.allow_tf32 = True
                    torch.set_float32_matmul_precision("high")
                except Exception:
                    pass
        except Exception:
            pass


def request_stop_event(stop_event) -> None:
    try:
        stop_event.set()
    except Exception:
        pass


def install_signal_handlers(stop_event) -> None:
    def _handler(signum, frame):
        request_stop_event(stop_event)

    try:
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass

    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


def queue_put_best_effort(q, item, timeout: float = 0.2) -> bool:
    try:
        q.put(item, timeout=timeout)
        return True
    except Exception:
        return False


def queue_put_drop_oldest(q, item) -> bool:
    try:
        q.put_nowait(item)
        return True
    except Exception:
        try:
            q.get_nowait()
        except Exception:
            pass
        try:
            q.put_nowait(item)
            return True
        except Exception:
            return False


@dataclass(frozen=True)
class MpPaths:
    output_dir: Path
    temp_segments_dir: Path
    previews_dir: Path
    incidents_dir: Path

    @classmethod
    def from_output_dir(cls, output_dir: str | Path) -> "MpPaths":
        out = Path(output_dir)
        return cls(
            output_dir=out,
            temp_segments_dir=out / "temp_segments",
            previews_dir=out / "previews",
            incidents_dir=out / "incidents",
        )

    def mkdirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_segments_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.incidents_dir.mkdir(parents=True, exist_ok=True)