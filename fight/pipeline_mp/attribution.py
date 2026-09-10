"""Best-effort attribution only; never an input to admission, health or EOF."""
from contextlib import contextmanager
import math
import time

from fight.pipeline_mp.common import now_str
from fight.pipeline_mp.messages import ReportMessage
from fight.pipeline_mp.performance import make_timing_collectors
from fight.runtime_supervisor.camera_state import MAX_CAMERAS


class AttributionMetrics:
    def __init__(self, runtime, timings=(), counters=(), *, clock=None):
        self.enabled = bool(runtime.get("performance_metrics_enabled", False))
        self.clock = clock or time.perf_counter
        try:
            interval = float(runtime.get("performance_attribution_report_interval_sec", 30))
        except (TypeError, ValueError, OverflowError):
            interval = 30.0
        self.report_interval_sec = max(5.0, interval) if math.isfinite(interval) else 30.0
        self.timings = make_timing_collectors(runtime, timings)
        self.totals = dict.fromkeys(timings, 0.0)
        self.counters = dict.fromkeys(counters, 0)
        self.last_report = None
        self.reports_dropped = 0

    def count(self, name, value=1):
        if self.enabled and name in self.counters:
            self.counters[name] += value

    def observe(self, name, milliseconds):
        if self.enabled and name in self.timings:
            try:
                self.timings[name].observe(milliseconds)
                self.totals[name] += max(0.0, milliseconds)
            except Exception:
                pass

    def start_timer(self):
        if self.enabled:
            try:
                return self.clock()
            except Exception:
                pass
        return None

    def finish_timer(self, name, start):
        """Record only when the caller confirms completion; clock failures are harmless."""
        if start is not None:
            try:
                self.observe(name, (self.clock() - start) * 1000)
            except Exception:
                pass

    @contextmanager
    def measure(self, name, *, excluding=()):
        start = None
        if self.enabled:
            try:
                start = self.clock()
                excluded = sum(self.totals[key] for key in excluding)
            except Exception:
                start = None
        try:
            yield
        finally:
            if start is not None:
                try:
                    elapsed = (self.clock() - start) * 1000
                    elapsed -= sum(self.totals[key] for key in excluding) - excluded
                    self.observe(name, elapsed)
                except Exception:
                    pass  # Telemetry must not mask an application exception.

    def snapshot(self):
        return {"enabled": self.enabled, "counters": dict(self.counters),
                "timings": {name: collector.summary() if collector.sample_count else None
                            for name, collector in self.timings.items()},
                "reports_dropped": self.reports_dropped,
                "scope": "bounded tail from this process incarnation; wall time, not GPU kernel time"}

    def publish(self, channel, component, *, force=False, **identity):
        if not self.enabled or channel is None:
            return False
        try:
            now = self.clock()
            if not force and self.last_report is not None and now - self.last_report < self.report_interval_sec:
                return False
            self.last_report = now
            channel.put_nowait(ReportMessage("status", {
                "ts": now_str(), "stage": "attribution", "detail": "summary", "component": component,
                "camera_id": "__system__", **identity, "metrics": self.snapshot(),
            }))
            return True
        except Exception:
            self.reports_dropped += 1
            return False


def attribution_summary(config, rows):
    """Keep only the latest report for configured cameras/components; no percentile merging."""
    cameras = {str(camera["camera_id"]) for camera in config.get("cameras", [])
               if camera.get("camera_id") is not None}
    result = {"vehicle": None, **{name: {cid: None for cid in sorted(cameras)}
              for name in ("camera_ingest", "fight_local", "speed_local", "preview")}}
    latest = {}
    for row in rows:
        if row.get("stage") != "attribution" or row.get("detail") != "summary":
            continue
        retain_attribution(latest, row)
    for row in latest.values():
        name, cid = row.get("component"), row.get("camera_id")
        payload = {key: row[key] for key in ("metrics", "generation", "consumer_epoch", "service_epoch") if key in row}
        if name == "vehicle":
            result[name] = payload
        elif name in result and cid in cameras:
            result[name][cid] = payload
    result["unavailable"] = {"pure_ipc_copy_ms": None, "gpu_kernel_ms": None,
                             "incident_end_to_end_ms": None}
    return result


def retain_attribution(cache, row):
    """Bound only NEW attribution rows when scanning the existing status JSONL.

    Keep latest-incarnation summaries, never accumulate the periodic history.
    This does not change durable incident/cursor or legacy status semantics.
    """
    key = (row.get("component"), row.get("camera_id"))
    rank = lambda value: tuple(value.get(name, 0) for name in ("generation", "consumer_epoch", "service_epoch"))
    if key in cache and rank(row) < rank(cache[key]):
        return
    cache.pop(key, None)
    cache[key] = row
    if len(cache) > 4 * MAX_CAMERAS + 1:
        cache.pop(next(iter(cache)))
