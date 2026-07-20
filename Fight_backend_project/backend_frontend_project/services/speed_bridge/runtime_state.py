from __future__ import annotations

import threading
from typing import Optional

from .speed_runner import ActiveSpeedRun


class SpeedPipelineRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._active_run: Optional[ActiveSpeedRun] = None

    def get(self) -> Optional[ActiveSpeedRun]:
        with self._lock:
            return self._active_run

    def set(self, active_run: Optional[ActiveSpeedRun]) -> None:
        with self._lock:
            self._active_run = active_run

    def clear(self) -> None:
        with self._lock:
            self._active_run = None


speed_runtime = SpeedPipelineRuntime()