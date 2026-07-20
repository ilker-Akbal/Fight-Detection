from __future__ import annotations

from pathlib import Path

import cv2


def _is_file_source(source_url: str) -> bool:
    s = str(source_url).strip()
    if not s:
        return False

    if s.isdigit():
        return False

    low = s.lower()
    if low.startswith(("rtsp://", "rtmp://", "http://", "https://", "udp://", "tcp://")):
        return False

    return Path(s).exists()


class CameraReader:
    def __init__(self, source_url: str):
        self.source_url = str(source_url)
        self.cap = None
        self.is_file = _is_file_source(self.source_url)
        self.eof = False
        self.open()

    def open(self):
        self.close()
        self.eof = False

        if self.source_url.isdigit():
            self.cap = cv2.VideoCapture(int(self.source_url), cv2.CAP_DSHOW)
        else:
            self.cap = cv2.VideoCapture(self.source_url)

        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def read(self):
        if self.cap is None or not self.cap.isOpened():
            return False, None

        ok, frame = self.cap.read()
        if not ok or frame is None:
            if self.is_file:
                self.eof = True
            return False, None

        return True, frame

    def reconnect(self):
        if self.is_file:
            return
        self.open()

    def close(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None