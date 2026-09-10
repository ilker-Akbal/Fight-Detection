"""Normal service EOF, unlike failure recovery, must preserve worker summaries."""
import multiprocessing as mp
import json
import queue
import threading
import time
from functools import partial
from unittest.mock import Mock

import pytest

from fight.pipeline_mp.messages import PersonInferenceRequest
from fight.pipeline_mp.performance import build_performance_summary, load_status_rows
from fight.pipeline_mp.person_worker import run_person_inference_loop
from fight.pipeline_mp.reporter import reporter_process_main
from fight.pipeline_mp.run_multiprocess import _put_sentinel, _terminate_process
from fight.pipeline_mp.shared_services import SharedServices
from benchmarks.real_inference import stage_latency_summary
from benchmarks.telemetry import Output
from tests.test_shared_services import fixture
from tests.test_dynamic_camera_lifecycle import FakeProcess
from tests.test_speed_integration import camera


class FinalizationAdapter:
    def __init__(self, *_, ready):
        assert ready.wait(10)

    def detect_persons(self, frame):
        return [(0.9, (1, 2, 3, 4))]

    def detect_persons_batch(self, frames):
        return [self.detect_persons(frame) for frame in frames]


def spawned_person(config, requests, results, reports, stop, generations, health, ready):
    run_person_inference_loop(config, requests, results, reports, stop,
        adapter_factory=partial(FinalizationAdapter, ready=ready),
        slot_generations=generations, health_queue=health)


@pytest.mark.parametrize("batch,enabled,withdrawal", [(False, True, "close"),
    (True, True, "capability"), (False, False, "close")])
def test_spawned_person_final_summary_persists_before_reporter_exit(tmp_path, batch, enabled, withdrawal):
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    def spawn(name, target, args):
        if name == "person":
            target, args = spawned_person, (*args, ready)
        process = ctx.Process(name=name, target=target, args=args)
        process.start()
        return process
    runtime = {"use_pose": False, "use_stage3": False, "performance_metrics_enabled": enabled,
               "performance_metrics_warmup_requests": 0, "shared_service_idle_grace_sec": 0}
    if batch:
        runtime.update(person_batch_enabled=True, person_batch_size=2, person_batch_max_wait_ms=100)
    services, manager, _, _ = fixture(runtime, ctx=ctx, factory=spawn)
    config = manager.config
    config.update(output_dir=str(tmp_path), models={"yolo_config": "fake", "yolo_weights": "fake"})
    services.terminate = _terminate_process
    global_stop = ctx.Event()
    reporter = ctx.Process(target=reporter_process_main, args=(config, manager.report_queue, global_stop))
    reporter.start()
    processes = []
    try:
        services.prepare([camera("fight", True, False)])
        processes = list(services.processes().values())
        for slot in (0, 1):
            manager.slot_generations[slot] = 1
            manager.person_request_queue.put(PersonInferenceRequest(str(slot), 1, 1, 1, None,
                slot_id=slot, put_started_monotonic=time.perf_counter()), timeout=2)
        ready.set()
        for slot in (0, 1):
            assert manager.person_result_channels[slot].get(timeout=10).detections
        global_stop.set()  # Reporter must ignore the runtime stop event until its sentinel.
        if withdrawal == "capability":
            services.prepare([])
            services.tick()
            if services.draining is not None:
                assert services.draining.wait(2)
                services.tick()
            assert not services.bundles
        else:
            services.close()
        assert all(process.exitcode == 0 for process in processes)
        assert reporter.is_alive()
        assert _put_sentinel(manager.report_queue, reporter, timeout=2)
        reporter.join(10)
        assert reporter.exitcode == 0
        rows = load_status_rows(tmp_path / "camera_status.jsonl")
        worker = next(row for row in rows if row.get("stage") == "person_inference" and row.get("detail") == "summary")
        stopped = next(index for index, row in enumerate(rows)
                       if row.get("stage") == "reporter" and row.get("detail") == "stopped")
        assert rows.index(worker) < stopped
        assert worker["requests_processed"] == 2
        summary = build_performance_summary(config, rows, 1)["person"]
        for key in ("queue_wait_ms", "inference_ms", "result_enqueue_ms"):
            assert bool(summary[key]["samples"]) is enabled
            assert summary["worker_timings"]["all_requests"][key] == worker["all_requests"][key]
            assert summary["steady_state"][key] == worker["steady_state"][key]
        assert summary["batch"] == worker["batch"]
        assert summary["batch"]["enabled"] is batch
        for key in ("batch_size", "requests_per_batch", "batch_collect_wait_ms", "batch_inference_ms"):
            assert bool(summary["batch"][key]["samples"]) is enabled
        if batch:
            assert summary["batch"]["batch_size"]["max"] == 2
        output = Output(tmp_path / "benchmark", {})
        try:
            output.add({"mode": "real_inference", "stages": {"person": {
                "latency": stage_latency_summary(summary)}}})
            exported = json.loads((output.path / "benchmark_summary.json").read_text())
            latency = exported["real_inference"][0]["stages"]["person"]["latency"]
            assert latency["batch"] == summary["batch"]
            assert latency["worker_timings"] == summary["worker_timings"]
            assert latency["result_enqueue_ms"] == summary["result_enqueue_ms"]
        finally:
            output.close()
    finally:
        ready.set()
        services.close(graceful=False)
        for process in processes:
            _terminate_process(process, timeout=.2)
        if reporter.is_alive():
            _put_sentinel(manager.report_queue, reporter, timeout=.2)
            _terminate_process(reporter, timeout=1)
        for channel in (manager.report_queue, services.incident_queue):
            SharedServices._close(channel)


def test_failed_withdrawal_never_enters_graceful_finalization():
    services, manager, _, _ = fixture()
    services.prepare([camera("fight", True, False)])
    services._finalize = Mock(side_effect=AssertionError("failure must not drain"))
    processes = list(services.processes().values())
    services._fight_failure("person_process_dead")
    assert all(not process.is_alive() for process in processes)
    services._finalize.assert_not_called()
    services.close(graceful=False)


@pytest.mark.parametrize("file_eof", [False, True])
def test_dynamic_parent_finalizes_worker_then_reporter_then_summary(tmp_path, monkeypatch, file_eof):
    from fight.pipeline_mp import run_multiprocess as runtime
    from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    ready.set()
    started, managers, stop = {}, [], []
    def spawn(name, target, args):
        if name in {"person", "person_router", "reporter"}:
            if name == "person":
                target, args = spawned_person, (*args, ready)
            process = ctx.Process(name=name, target=target, args=args)
            process.start()
        else:
            process = FakeProcess(name)
        started[name] = process
        return process
    original_init = CameraRuntimeManager.__init__
    def init(manager, **kwargs):
        original_init(manager, **kwargs)
        managers.append(manager)
    worked = []
    def drive(_):
        if worked:
            return
        manager = managers[0]
        item = manager.runtimes["cam"]
        manager.person_request_queue.put(PersonInferenceRequest("cam", item.generation, 1, 1, None,
            slot_id=item.slot_id, put_started_monotonic=time.perf_counter()), timeout=2)
        assert manager.person_result_channels[item.slot_id].get(timeout=10).detections
        worked.append(True)
        if file_eof:
            item.file_eof_event.set()  # Authoritative marker, never a telemetry event.
            for process in item.processes.values():
                process.alive, process.exitcode = False, 0
        else:
            stop[0].set()
    original_sentinel = runtime._put_sentinel
    ordered = []
    def sentinel(channel, process, **kwargs):
        if process is started["reporter"]:
            assert started["person"].exitcode == started["person_router"].exitcode == 0
            ordered.append("reporter_sentinel")
        return original_sentinel(channel, process, **kwargs)
    original_summary = runtime.build_performance_summary
    def summarize(*args, **kwargs):
        assert ordered == ["reporter_sentinel"]
        assert started["reporter"].exitcode == 0  # Its finally flush already completed.
        ordered.append("summary")
        return original_summary(*args, **kwargs)
    monkeypatch.setattr(runtime, "_start_process", spawn)
    monkeypatch.setattr(CameraRuntimeManager, "_default_process_factory", staticmethod(spawn))
    monkeypatch.setattr(CameraRuntimeManager, "__init__", init)
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda event: stop.append(event))
    monkeypatch.setattr(runtime.time, "sleep", drive)
    monkeypatch.setattr(runtime, "_wait_for_pipeline_settle", lambda **_: None)
    monkeypatch.setattr(runtime, "_put_sentinel", sentinel)
    monkeypatch.setattr(runtime, "build_performance_summary", summarize)
    monkeypatch.setattr(runtime, "_close_queue", SharedServices._close)
    source = tmp_path / "file.mp4"
    source.touch()
    config = {"output_dir": str(tmp_path / "run"), "cameras": [camera("cam", True, False,
        str(source) if file_eof else "0")], "models": {"yolo_config": "fake", "yolo_weights": "fake"},
        "runtime": {"use_pose": False, "use_stage3": False, "health_enabled": False,
                    "dynamic_camera_slot_count": 1, "performance_metrics_enabled": True}}
    try:
        assert runtime._run_dynamic(config) == 0
        summary = json.loads((tmp_path / "run" / "performance_summary.json").read_text())
        assert summary["person"]["inference_ms"]["samples"] == 1
        assert summary["person"]["result_enqueue_ms"]["samples"] == 1
        assert summary["person"]["batch"]["enabled"] is False
        assert ordered == ["reporter_sentinel", "summary"]
    finally:
        for process in started.values():
            _terminate_process(process, timeout=.2)


def test_unhealthy_admission_and_worker_cannot_block_normal_close(monkeypatch):
    services, manager, _, _ = fixture({"use_pose": False, "use_stage3": False})
    services.prepare([camera("fight", True, False)])
    bundle = services.bundles["fight"]
    release = threading.Event()
    entered = threading.Event()
    def unusable_transport(*_, **__):
        entered.set()
        release.wait(5)  # Simulate a poisoned admission lock, released in cleanup.
    monkeypatch.setattr(bundle["admissions"]["person"], "put", unusable_transport)
    monkeypatch.setattr(services, "FINALIZE_TIMEOUT_SEC", .05)
    process = bundle["processes"]["person"]
    process.join = Mock()  # Unresponsive process; only forced termination succeeds.
    started = time.monotonic()
    try:
        services.close()
        assert entered.is_set()
        assert time.monotonic() - started < 1
        assert not process.is_alive() and not services.bundles
        # Windows Event.wait can return slightly before the monotonic deadline.
        # The invariant is a bounded remaining budget, not an exact zero clock.
        assert 0 <= process.join.call_args.kwargs["timeout"] <= services.FINALIZE_TIMEOUT_SEC
    finally:
        release.set()
