"""Real parent-side structures with fake processes: NOT an inference benchmark."""
from __future__ import annotations

import copy
import json
import queue
import random
import threading
import time
from collections import deque

from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
from fight.pipeline_mp.generation import is_current_generation
from fight.pipeline_mp.health import HealthEvent, HealthPolicy, HealthRegistry
from fight.pipeline_mp.messages import PersonInferenceRequest
from fight.pipeline_mp.scheduling import FairRequestQueue
from fight.runtime_supervisor.camera_state import MAX_CAMERAS, validate_desired_state
from benchmarks.telemetry import distribution, rss_bytes


class ThreadContext:
    Queue = staticmethod(queue.Queue)
    Event = staticmethod(threading.Event)
    RLock = staticmethod(threading.RLock)

    @staticmethod
    def Array(_type, count, lock=False):
        return [0] * count


class FakeProcess:
    """No child target is executed and no OS PID is allocated."""
    pid = None
    exitcode = None

    def __init__(self, name, *_args):
        self.name = name
        self.alive = True

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False
        self.exitcode = 0


class BoundedReports:
    """A draining reporter substitute retaining only a bounded tail."""
    def __init__(self, capacity):
        self.rows = deque(maxlen=capacity)
        self.observations = 0

    def put(self, row, **_kwargs):
        self.observations += 1
        self.rows.append(row)


def run_control(count, *, seed=17, max_samples=256, workload="mixed", duration=1, warmup=0):
    if not 1 <= count <= MAX_CAMERAS or max_samples < 1 or duration <= 0 or warmup < 0:
        raise ValueError("invalid control-plane benchmark configuration")
    rng = random.Random(seed)
    started = time.perf_counter()
    before = rss_bytes()
    ctx = ThreadContext()
    stages = {name: FairRequestQueue(ctx, count) for name in ("person", "pose", "stage3", "vehicle")}
    reports = BoundedReports(max_samples)
    generations, epochs = [0] * count, [0] * count
    manager = CameraRuntimeManager(
        ctx=ctx, config={"output_dir": ".", "runtime": {"camera_ingest_mode": "centralized"}},
        stage3_queue=stages["stage3"], report_queue=reports,
        person_request_queue=stages["person"], person_result_channels={i: queue.Queue(2) for i in range(count)},
        pose_request_queue=stages["pose"], pose_result_channels={i: queue.Queue(2) for i in range(count)},
        vehicle_requests=stages["vehicle"], vehicle_results={i: queue.Queue(2) for i in range(count)},
        speed_epochs=epochs, slot_generations=generations, process_factory=FakeProcess,
        terminate_process=lambda process, **_: process.terminate(), close_queue=lambda _: None)
    cameras = [{"camera_id": f"bench-{i:04d}", "source": f"synthetic://camera/{i}", "enabled": True,
                "use_fight_detection": workload != "speed", "use_speed_detection": workload != "fight"}
               for i in range(count)]
    timings = {}
    def measure(name, function):
        start = time.perf_counter()
        result = function()
        timings[name] = (time.perf_counter() - start) * 1000
        return result
    try:
        desired = measure("desired_validation_ms", lambda: validate_desired_state(
            {"schema_version": 1, "revision": 1, "cameras": cameras}))
        measure("initial_reconcile_ms", lambda: manager.reconcile(desired["cameras"], revision=1))
        assert len(manager.runtimes) == count
        original = {cid: (item.slot_id, item.generation, item) for cid, item in manager.runtimes.items()}
        # A sorted subset is removed and restored: slots not removed must remain untouched.
        removed = set(rng.sample(list(original), max(1, count // 4)))
        remaining = [camera for camera in cameras if camera["camera_id"] not in removed]
        measure("remove_ms", lambda: manager.reconcile(remaining, revision=2))
        measure("readd_ms", lambda: manager.reconcile(cameras, revision=3))
        unchanged = all(manager.runtimes[cid] is old[2] for cid, old in original.items() if cid not in removed)
        slots_unique = len({item.slot_id for item in manager.runtimes.values()}) == count
        readd_slots_stable = all(manager.runtimes[cid].slot_id == original[cid][0] for cid in removed)
        generations_advanced = all(manager.runtimes[cid].generation > original[cid][1] for cid in removed)
        stale_fenced = all(not is_current_generation(PersonInferenceRequest(cid, old[1], 1, 1, None,
                                slot_id=old[0]), generations) for cid, old in original.items() if cid in removed)
        transitioned = copy.deepcopy(cameras)
        transitioned[0]["use_speed_detection"] = not cameras[0]["use_speed_detection"]
        transitioned[0]["use_fight_detection"] = True
        first = manager.runtimes[cameras[0]["camera_id"]]
        first_generation = first.generation
        first_processes = dict(first.processes)
        peer_objects = {cid: item for cid, item in manager.runtimes.items() if cid != first.camera_id}
        measure("capability_transition_ms", lambda: manager.reconcile(transitioned, revision=4))
        replacement = manager.runtimes[first.camera_id]
        transition_safe = (replacement is first and replacement.generation == first_generation
                           and all(replacement.processes[name] is first_processes[name]
                                   for name in ("ingest", "preview"))
                           and ("camera" not in first_processes or
                                replacement.processes["camera"] is first_processes["camera"])
                           and ("speed" in replacement.processes) == transitioned[0]["use_speed_detection"]
                           and ("speed" not in first_processes or not first_processes["speed"].is_alive())
                           and all(manager.runtimes[cid] is item for cid, item in peer_objects.items()))
        registry = HealthRegistry(monotonic=lambda: 100, transition_limit=max_samples, max_cameras=count)
        registry.sync_cameras(manager.get_camera_status())
        for name in stages:
            registry.register_worker(name)
            registry.handle(HealthEvent(name, "shared_worker", "heartbeat", 100))
        for item in manager.runtimes.values():
            for component in ("camera_ingest", "camera_worker", "speed_worker"):
                registry.handle(HealthEvent(component, "camera", "frame_progress", 100,
                    camera_id=item.camera_id, slot_id=item.slot_id, generation=item.generation, progress=1,
                    consumer_epoch=item.fight_epoch if component == "camera_worker" else
                                   item.speed_epoch if component == "speed_worker" else 0))
        measurements = deque(maxlen=max_samples)
        cycles = 0
        # Positive sleep keeps long synthetic measurements from becoming a busy loop.
        phase_start = time.perf_counter()
        while time.perf_counter() - phase_start < warmup + duration:
            cycle_start = time.perf_counter()
            manager.reconcile(transitioned, revision=4)
            reconcile_ms = (time.perf_counter() - cycle_start) * 1000
            health_start = time.perf_counter()
            registry.sync_cameras(manager.get_camera_status())
            registry.evaluate(HealthPolicy(), now=100)
            snapshot = registry.snapshot("synthetic", now=100)
            snapshot_bytes = len(json.dumps(snapshot))
            health_ms = (time.perf_counter() - health_start) * 1000
            if cycle_start - phase_start >= warmup:
                cycles += 1
                measurements.append({"reconcile_ms": reconcile_ms, "health_snapshot_evaluation_ms": health_ms})
            time.sleep(.01)
        fairness = {}
        for name, admission in stages.items():
            order = []
            for _ in range(2):
                for item in manager.runtimes.values():
                    request = PersonInferenceRequest(item.camera_id, item.generation, 1, 1, None, slot_id=item.slot_id)
                    admission.put_nowait(request)
                    try:
                        admission.put_nowait(request)
                    except queue.Full:
                        pass
                for _ in range(count):
                    order.append(admission.get_nowait().slot_id)
                    admission.task_done()
            fairness[name] = {"round_robin_correct": order == list(range(count)) * 2,
                              "dispatches_per_slot": 2, **admission.snapshot()}
        after = rss_bytes()
        return {"mode": "control_plane", "run_id": f"control-{count}-{seed}", "camera_equivalents": count,
                "workload": workload, "seed": seed, "real_inference": False,
                "duration_sec": time.perf_counter() - started, "requested_measurement_sec": duration,
                "warmup_sec": warmup, "timings": timings,
                "steady_cycles": {key: distribution(row[key] for row in measurements)
                                  for key in ("reconcile_ms", "health_snapshot_evaluation_ms")},
                "rss_delta_bytes_approx": after - before if after is not None and before is not None else None,
                "correctness": {"unchanged_cameras_preserved": unchanged, "slots_unique": slots_unique,
                                "readd_slots_stable": readd_slots_stable,
                                "readd_generations_advanced": generations_advanced, "stale_results_fenced": stale_fenced,
                                "capability_transition_safe": transition_safe},
                "scheduler": fairness, "snapshot_bytes": snapshot_bytes,
                "telemetry": {"capacity": max_samples, "cycle_observations": cycles,
                              "cycles_retained": len(measurements), "reports_observed": reports.observations,
                              "reports_retained": len(reports.rows), "transitions_retained": len(registry.transitions)}}
    finally:
        manager.stop_all()
