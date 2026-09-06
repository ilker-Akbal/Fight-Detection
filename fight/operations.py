"""Small ORM-free durability and operational status primitives."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import time
from pathlib import Path


def run_lock_path(path):
    root = Path(path).resolve()
    key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    return root.parent / ".run_locks" / (key + ".lock")


def read_small_json(path):
    with Path(path).open("rb") as handle:
        data = handle.read(65537)
    if len(data) > 65536:
        raise ValueError("oversized operational state")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid operational state")
    return value


def fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    fsync_directory(target.parent)


class DiskMonitor:
    def __init__(self, runtime=None, *, usage=shutil.disk_usage, monotonic=time.monotonic):
        runtime = runtime or {}
        self.warning = max(0, int(runtime.get("disk_warning_bytes", 5 * 1024**3)))
        self.critical = max(0, int(runtime.get("disk_critical_bytes", 1024**3)))
        self.warning = max(self.warning, self.critical)
        self.interval = max(1, float(runtime.get("disk_check_interval_sec", 30)))
        self.usage, self.monotonic = usage, monotonic
        self._checked = -1e30
        self._snapshot = {}

    def sample(self, roots):
        now = self.monotonic()
        if now - self._checked < self.interval:
            return self._snapshot
        volumes = {}
        for name, root in roots.items():
            path = Path(root)
            while not path.exists() and path != path.parent:
                path = path.parent
            try:
                usage = self.usage(path)
                state = ("CRITICAL" if usage.free <= self.critical else
                         "WARNING" if usage.free <= self.warning else "OK")
                volumes[name] = {"state": state, "free_bytes": usage.free,
                                 "total_bytes": usage.total}
            except OSError as exc:
                volumes[name] = {"state": "UNKNOWN", "errno": exc.errno}
        rank = {"OK": 0, "UNKNOWN": 1, "WARNING": 2, "CRITICAL": 3}
        state = max((item["state"] for item in volumes.values()), key=rank.get, default="UNKNOWN")
        self._snapshot = {"state": state, "volumes": volumes,
                          "warning_bytes": self.warning, "critical_bytes": self.critical}
        self._checked = now
        return self._snapshot
