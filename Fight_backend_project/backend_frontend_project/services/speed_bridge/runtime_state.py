from __future__ import annotations

import threading
from typing import Optional

from .speed_runner import ActiveSpeedRun, get_active_speed_run


class SpeedPipelineRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._active_run: Optional[ActiveSpeedRun] = None

    def get(self) -> Optional[ActiveSpeedRun]:
        # Supervisor state, not the current Django/Gunicorn process's cache,
        # determines whether Speed is running.
        return get_active_speed_run()

    def set(self, active_run: Optional[ActiveSpeedRun]) -> None:
        with self._lock:
            self._active_run = active_run

    def clear(self) -> None:
        with self._lock:
            self._active_run = None


speed_runtime = SpeedPipelineRuntime()
