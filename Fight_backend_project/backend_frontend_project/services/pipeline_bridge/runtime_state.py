from __future__ import annotations

import threading
from typing import Optional

from .fight_runner import ActiveRun


class PipelineRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._active_run: Optional[ActiveRun] = None

    def get(self) -> Optional[ActiveRun]:
        with self._lock:
            return self._active_run

    def set(self, active_run: Optional[ActiveRun]) -> None:
        with self._lock:
            self._active_run = active_run


runtime = PipelineRuntime()