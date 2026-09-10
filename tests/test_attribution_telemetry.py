"""Phase 18 accounting is deterministic, bounded, and independent of correctness."""
import json
import queue
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from fight.pipeline_mp.attribution import AttributionMetrics, attribution_summary
from fight.pipeline_mp.camera_ingest import run_camera_ingest_loop
from fight.pipeline_mp.camera_worker import CameraProcessRunner
from fight.pipeline_mp.health import HealthEmitter
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal
from fight.pipeline_mp.performance import build_performance_summary, load_status_rows
from fight.runtime_supervisor.camera_state import MAX_CAMERAS
from fight.pipeline_mp.scheduling import FairRequestQueue
from fight.pipeline_mp.speed_worker import VehicleClient, VehicleRequest, VehicleResult, vehicle_service_main, speed_process_main
from benchmarks.telemetry import Output
from tests.test_camera_ingest import FakeCapture, _drain
from tests.test_capacity_scheduling import ThreadContext


RUNTIME = {"performance_metrics_enabled": True, "performance_metrics_max_samples": 2}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def last_metrics(reports, component):
    return [msg.row["metrics"] for msg in _drain(reports)
            if msg.row.get("component") == component][-1]


def test_bounded_nested_timing_drops_and_exceptions_never_control_work():
    clock = Clock()
    telemetry = AttributionMetrics(RUNTIME, ("local", "shared"), clock=clock)
    for _ in range(5):
        with telemetry.measure("local", excluding=("shared",)):
            clock.advance(.002)
            with telemetry.measure("shared"):
                clock.advance(.100)
            clock.advance(.003)
    assert telemetry.snapshot()["timings"]["local"]["mean"] == 5
    assert telemetry.snapshot()["timings"]["shared"]["observations"] == 5
    assert telemetry.snapshot()["timings"]["shared"]["samples"] == 2
    full = queue.Queue(1)
    full.put("occupied")
    assert not telemetry.publish(full, "vehicle", force=True)
    assert telemetry.reports_dropped == 1
    full.get_nowait()
    assert telemetry.publish(full, "vehicle", force=True)
    assert full.get_nowait().row["metrics"]["reports_dropped"] == 1
    assert not telemetry.publish(full, "vehicle")  # Rate limited even with space.
    telemetry.clock = Mock(side_effect=RuntimeError("telemetry clock"))
    assert telemetry.start_timer() is None
    telemetry.finish_timer("local", clock())  # An end-clock failure is harmless too.
    with pytest.raises(ValueError, match="application"):
        with telemetry.measure("local"):
            raise ValueError("application")
    assert not telemetry.publish(full, "vehicle", force=True)
    disabled = AttributionMetrics({}, ("local",), clock=Mock(side_effect=AssertionError))
    with disabled.measure("local"):
        pass
    assert disabled.snapshot()["timings"]["local"] is None


def test_report_interval_is_configurable_clamped_and_local():
    for value, expected in ((None, 30), (60, 60), (0, 5), (-1, 5),
                            ("invalid", 30), (float("nan"), 30), (float("inf"), 30)):
        clock = Clock()
        runtime = dict(RUNTIME)
        if value is not None:
            runtime["performance_attribution_report_interval_sec"] = value
        telemetry = AttributionMetrics(runtime, clock=clock)
        reports = queue.Queue(1)
        assert telemetry.report_interval_sec == expected
        assert telemetry.publish(reports, "vehicle")
        datetime.strptime(reports.get_nowait().row["ts"], "%Y-%m-%d %H:%M:%S.%f")
        clock.advance(expected - 1)
        assert not telemetry.publish(reports, "vehicle")
        other = AttributionMetrics(runtime, clock=clock)
        assert other.publish(reports, "vehicle")  # Independent producer.
        clock.advance(1)
        assert not telemetry.publish(reports, "vehicle")  # Full queue at interval boundary.
        assert telemetry.reports_dropped == 1
        reports.get_nowait()
        assert not telemetry.publish(reports, "vehicle")  # Failed attempt consumed interval.
        clock.advance(expected)
        assert telemetry.publish(reports, "vehicle")
        reports.get_nowait()
        assert telemetry.publish(reports, "vehicle", force=True)


def test_vehicle_service_latency_and_counts_preserve_fair_admission(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.perf_counter", clock)
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.configure_process_runtime", lambda **_: None)
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.speed_config", lambda *_: (SimpleNamespace(yolo=None), None))
    admission = FairRequestQueue(ThreadContext(), 1, 2)
    admission.put(VehicleRequest("A", 0, 1, 1, 1, object(), 99, True, 2, submitted_monotonic=99.95))
    admission.put(None)
    reports, results = queue.Queue(), {0: queue.Queue(2)}
    original_put = results[0].put
    attempts = []
    def retry_put(result, timeout):
        attempts.append(result)
        clock.advance(.100 if len(attempts) < 3 else .025)
        if len(attempts) < 3:
            raise queue.Full
        original_put(result, timeout=timeout)
    monkeypatch.setattr(results[0], "put", retry_put)
    def detect(_):
        clock.advance(.020)
        return ["detection"]
    vehicle_service_main({"runtime": RUNTIME}, admission, results, threading.Event(), [1], [1],
        detector_factory=lambda _: SimpleNamespace(detect=detect), report_queue=reports)
    metrics = last_metrics(reports, "vehicle")
    assert metrics["counters"]["requests_accepted"] == metrics["counters"]["requests_completed"] == 1
    assert metrics["counters"]["inferences_completed"] == 1
    assert metrics["timings"]["queue_wait_inclusive_ms"]["mean"] == 50
    assert metrics["timings"]["inference_ms"]["mean"] == 20
    assert len(attempts) == 3
    assert metrics["timings"]["result_enqueue_ms"]["observations"] == 1
    assert metrics["timings"]["result_enqueue_ms"]["mean"] == 225
    assert results[0].get_nowait().detections == ["detection"]
    assert admission.snapshot()["outstanding"] == 0


@pytest.mark.parametrize("invalidated", ("stop", "generation", "epoch"))
@pytest.mark.parametrize("when", ("after_inference", "during_retry"))
def test_vehicle_abandoned_delivery_is_not_completed(monkeypatch, invalidated, when):
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.configure_process_runtime", lambda **_: None)
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.speed_config", lambda *_: (SimpleNamespace(yolo=None), None))
    admission = FairRequestQueue(ThreadContext(), 1, 2)
    admission.put(VehicleRequest("A", 0, 1, 1, 1, object(), 99, True, 2))
    admission.put(None)
    stop, generations, epochs = threading.Event(), [1], [1]
    def invalidate():
        if invalidated == "stop":
            stop.set()
        else:
            (generations if invalidated == "generation" else epochs)[0] = 2
    def detect(_):
        if when == "after_inference":
            invalidate()
        return []
    def full(*_args, **_kwargs):
        invalidate()
        raise queue.Full
    result_queue = Mock(put=Mock(side_effect=full))
    reports = queue.Queue()
    vehicle_service_main({"runtime": RUNTIME}, admission, {0: result_queue}, stop, generations, epochs,
        detector_factory=lambda _: SimpleNamespace(detect=detect), report_queue=reports)
    metrics = last_metrics(reports, "vehicle")
    assert metrics["counters"]["requests_accepted"] == 1
    assert metrics["counters"]["inferences_completed"] == 1
    assert metrics["counters"]["requests_completed"] == 0
    assert metrics["timings"]["result_enqueue_ms"] is None
    assert result_queue.put.call_count == (1 if when == "during_retry" else 0)


@pytest.mark.parametrize("stale", ("live", "generation"))
def test_vehicle_stale_completion_requires_delivered_result(monkeypatch, stale):
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.configure_process_runtime", lambda **_: None)
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.live_request_stale", lambda _: True)
    admission = FairRequestQueue(ThreadContext(), 1, 2)
    admission.put(VehicleRequest("A", 0, 1, 1, 1, object(), 1, False, 2))
    admission.put(None)
    reports, results = queue.Queue(), queue.Queue()
    detector_factory = Mock(side_effect=AssertionError("stale work must not infer"))
    vehicle_service_main({"runtime": RUNTIME}, admission, {0: results}, threading.Event(),
        [2 if stale == "generation" else 1], [1], detector_factory=detector_factory, report_queue=reports)
    metrics = last_metrics(reports, "vehicle")
    assert metrics["counters"]["stale_" + stale] == 1
    assert metrics["counters"]["inferences_completed"] == 0
    assert metrics["counters"]["requests_completed"] == (1 if stale == "live" else 0)
    if stale == "live":
        assert results.get_nowait().outcome == "stale"
        assert metrics["timings"]["result_enqueue_ms"]["observations"] == 1
    else:
        assert metrics["timings"]["result_enqueue_ms"] is None
        with pytest.raises(queue.Empty):
            results.get_nowait()


def test_vehicle_client_round_trip_does_not_repurpose_capture_clock(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.perf_counter", clock)
    source = tmp_path / "file.mp4"
    source.touch()
    response = queue.Queue()
    def admitted(channel, request, *_args, **_kwargs):
        assert request.created_monotonic == 42  # Still the staleness/capture clock.
        assert request.submitted_monotonic == 100
        clock.advance(.003)
        response.put(VehicleResult(request, ["ok"]))
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.admit", admitted)
    original = response.get
    def receive(**kwargs):
        clock.advance(.007)
        return original(**kwargs)
    monkeypatch.setattr(response, "get", receive)
    client = VehicleClient(None, response, threading.Event(), HealthEmitter(None, component="speed_worker", component_type="camera"),
                           {"camera_id": "A", "source": str(source)}, 0, 1, 1, RUNTIME)
    assert client.detect(None, SimpleNamespace(frame_seq=1, captured_monotonic=42)) == ["ok"]
    timings = client.telemetry.snapshot()["timings"]
    assert timings["vehicle_enqueue_ms"]["mean"] == 3
    assert timings["vehicle_round_trip_ms"]["mean"] == 10
    assert timings["vehicle_call_ms"]["mean"] == 10


def test_ingest_read_fanout_and_offered_counts_do_not_own_eof(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.perf_counter", clock)
    source = tmp_path / "file.mp4"
    source.touch()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    capture = FakeCapture([frame])
    original = capture.read
    def read():
        clock.advance(.004)
        return original()
    capture.read = read
    reports, fight, speed, preview = queue.Queue(), queue.Queue(2), queue.Queue(2), queue.Queue(2)
    eof = threading.Event()
    opens = []
    def open_once(value):
        opens.append(value)
        return capture
    run_camera_ingest_loop({"runtime": RUNTIME}, {"camera_id": "A", "source": str(source)},
        fight, preview, reports, threading.Event(), 1, capture_factory=open_once,
        speed_channel=speed, file_eof_event=eof)
    metrics = last_metrics(reports, "camera_ingest")
    assert metrics["timings"]["read_ms"]["mean"] == 4
    assert metrics["timings"]["read_ms"]["observations"] == 2  # Includes EOF read.
    for name, channel in (("fight", fight), ("speed", speed), ("preview", preview)):
        assert metrics["counters"][name + "_offered"] == metrics["counters"][name + "_enqueued"] == 1
        assert metrics["timings"][name + "_enqueue_ms"] is not None
        assert [type(item) for item in _drain(channel)] == [CameraFrame, CameraIngestSignal]
    assert eof.is_set() and opens == [str(source)]
    # Refuse every report, including legacy reports; ordered data/EOF still work.
    reports.put_nowait = Mock(side_effect=queue.Full)
    reports.put = Mock(side_effect=queue.Full)
    eof.clear()
    run_camera_ingest_loop({"runtime": RUNTIME}, {"camera_id": "A", "source": str(source)},
        fight, preview, reports, threading.Event(), 2,
        capture_factory=lambda _: FakeCapture([frame]), file_eof_event=eof)
    assert eof.is_set() and _drain(fight)[-1].detail == "eof"


def test_fight_local_subtracts_shared_waits(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.perf_counter", clock)
    runner = CameraProcessRunner.__new__(CameraProcessRunner)
    runner.runtime, runner.report_queue = RUNTIME, queue.Queue()
    runner.camera_id, runner.generation, runner.frame_idx = "A", 1, 1
    runner.person_inference = SimpleNamespace(infer=lambda *_: (clock.advance(.100) or []))
    def local(_):
        clock.advance(.002)
        runner.detect_persons(None)
        clock.advance(.003)
    runner.process_frame = local
    runner.process_frame_measured(None)
    assert runner.attribution.snapshot()["timings"]["local_processing_ms"]["mean"] == 5
    assert runner.attribution.snapshot()["timings"]["person_call_ms"]["mean"] == 100


def test_speed_local_accounting_and_eof_are_independent(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.perf_counter", clock)
    monkeypatch.setattr("fight.pipeline_mp.speed_worker.configure_process_runtime", lambda **_: None)
    source = tmp_path / "file.mp4"
    source.touch()
    frames, reports = queue.Queue(), queue.Queue()
    frames.put(CameraFrame("A", 1, 1, clock(), 0, None))
    frames.put(CameraIngestSignal("A", 1, "eof"))
    def factory(config, camera, first, client, *_):
        def process(frame):
            clock.advance(.002)
            with client.telemetry.measure("vehicle_call_ms"):
                clock.advance(.100)
            clock.advance(.003)
        return SimpleNamespace(process=process)
    speed_process_main({"runtime": RUNTIME}, {"camera_id": "A", "source": str(source)}, frames,
        None, None, threading.Event(), 1, 0, [1], 1, [1], processor_factory=factory, report_queue=reports)
    metrics = last_metrics(reports, "speed_local")
    assert metrics["timings"]["local_processing_ms"]["mean"] == 5
    assert metrics["counters"]["frames_completed"] == metrics["counters"]["frames_received"] == 1


def test_summary_and_benchmark_export_preserve_nulls_and_latest_incarnation(tmp_path):
    assert attribution_summary({"cameras": [{}, {}]}, [])["fight_local"] == {}
    config = {"runtime": RUNTIME, "cameras": [{"camera_id": "A"}]}
    telemetry = AttributionMetrics(RUNTIME, ("inference_ms",))
    reports = queue.Queue()
    telemetry.publish(reports, "vehicle", force=True, service_epoch=1)
    telemetry.publish(reports, "vehicle", force=True, service_epoch=2)
    rows = [message.row for message in _drain(reports)]
    rows.append(rows[0])  # A late report from a withdrawn service cannot replace epoch 2.
    summary = build_performance_summary(config, rows, 1)["attribution"]
    assert summary["vehicle"]["service_epoch"] == 2
    assert summary["vehicle"]["metrics"]["timings"]["inference_ms"] is None
    assert summary["fight_local"]["A"] is None
    assert summary["unavailable"]["pure_ipc_copy_ms"] is None
    output = Output(tmp_path / "results", {})
    try:
        output.add({"mode": "real_inference", "attribution": summary})
        saved = json.loads((output.path / "benchmark_summary.json").read_text())
        assert saved["real_inference"][0]["attribution"] == summary
        assert saved["control_plane"] == []
    finally:
        output.close()


def test_periodic_status_scan_is_bounded_and_does_not_compact_legacy_history(tmp_path):
    path = tmp_path / "status.jsonl"
    row = {"stage": "attribution", "detail": "summary", "component": "vehicle", "camera_id": "__system__"}
    data = [json.dumps(row)] * 10000
    legacy = {"stage": "camera", "detail": "frame"}
    data += [json.dumps(legacy), json.dumps(legacy), '{"partial":']
    original = "\n".join(data)
    path.write_text(original)
    loaded = load_status_rows(path)
    assert len(loaded) == 3 and loaded[:2] == [legacy, legacy]
    assert path.read_text() == original
    from fight.pipeline_mp.attribution import retain_attribution
    cache = {}
    for index in range(4 * MAX_CAMERAS + 10):
        retain_attribution(cache, {**row, "camera_id": str(index)})
    assert len(cache) == 4 * MAX_CAMERAS + 1
