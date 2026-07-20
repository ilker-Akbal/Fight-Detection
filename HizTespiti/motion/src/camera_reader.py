from __future__ import annotations

import os
import time
from pathlib import Path

import cv2


def _is_int_source(source: str) -> bool:
    return str(source).strip().isdigit()


class CameraReader:
    def __init__(self, source: str, reconnect_wait_sec: float = 2.0):
        self.source = str(source)
        self.reconnect_wait_sec = reconnect_wait_sec
        self.cap: cv2.VideoCapture | None = None
        self.open()

    def open(self) -> None:
        self.close()

        if self.source.lower().startswith("rtsp://"):
            os.environ.setdefault(
                "OPENCV_FFMPEG_CAPTURE_OPTIONS",
                "rtsp_transport;tcp|stimeout;30000000",
            )

        if _is_int_source(self.source):
            self.cap = cv2.VideoCapture(int(self.source))
        else:
            path = Path(self.source)
            if path.exists():
                self.cap = cv2.VideoCapture(str(path))
            else:
                self.cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)

    def is_opened(self) -> bool:
        return self.cap is not None and self.cap.isOpened()

    def read(self):
        if self.cap is None or not self.cap.isOpened():
            self.reconnect()
            return False, None

        ok, frame = self.cap.read()

        if not ok or frame is None:
            if self.source.lower().startswith(("rtsp://", "http://", "https://")):
                self.reconnect()
            return False, None

        return True, frame

    def reconnect(self) -> None:
        self.close()
        time.sleep(self.reconnect_wait_sec)
        self.open()

    def fps(self) -> float:
        if self.cap is None:
            return 25.0
        fps = float(self.cap.get(cv2.CAP_PROP_FPS))
        if fps <= 1 or fps > 120:
            return 25.0
        return fps

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None