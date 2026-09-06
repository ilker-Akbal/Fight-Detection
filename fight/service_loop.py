"""Singleton, interruptible service loop; no Django or runtime dependencies."""
from __future__ import annotations

import signal
import threading

from fight.runtime_supervisor.locking import SingletonLock


def run_service(tick, lock_path, *, once=False, interval=1.0, report=lambda value: None,
                stop=None, max_backoff=60.0):
    stop = stop or threading.Event()
    interval = max(0.1, float(interval))
    previous = {}
    with SingletonLock(lock_path):
        try:
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, lambda *_: stop.set())
            failures = 0
            while not stop.is_set():
                try:
                    result = tick()
                    if result and (once or any(result.values())):
                        report(result)
                    failures = failures + 1 if result and result.get("deferred", 0) else 0
                except Exception as exc:
                    # Never include exception text: it can contain source URLs.
                    report({"service_error": type(exc).__name__, "errno": getattr(exc, "errno", None)})
                    if once:
                        raise
                    failures += 1
                if once:
                    return
                delay = min(max_backoff, interval * 2 ** min(failures, 10))
                stop.wait(max(interval, delay))
        except KeyboardInterrupt:
            stop.set()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
