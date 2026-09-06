from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
import unittest
from unittest.mock import Mock

from fight.pipeline_mp.batching import collect_request_batch
from fight.pipeline_mp.health import HealthEvent, HealthPolicy, HealthRegistry
from fight.pipeline_mp.messages import PersonInferenceRequest
from fight.pipeline_mp.person_worker import run_person_inference_loop
from fight.pipeline_mp.scheduling import (
    AdmissionShed, FairRequestQueue, admit, live_request_stale,
)


class ThreadContext:
    Queue = staticmethod(queue.Queue)
    Event = staticmethod(threading.Event)
    RLock = staticmethod(threading.RLock)

    @staticmethod
    def Array(_type, size, lock=False):
        return [0] * size


def request(slot, index=1, generation=1, *, file=True):
    return PersonInferenceRequest(
        f"camera-{slot}", generation, index, index, index,
        slot_id=slot, source_is_file=file, max_age_sec=2,
        created_monotonic=time.perf_counter() - 10,
    )


def consume_spawned(admission, result):
    rows = []
    while True:
        item = admission.get(timeout=5)
        admission.task_done()
        if item is None:
            break
        rows.append((item.slot_id, item.generation, item.request_id))
    result.put(rows)


class CapacitySchedulingTests(unittest.TestCase):
    def test_hot_camera_has_reserved_limit_and_round_robin_service(self):
        admission = FairRequestQueue(ThreadContext(), 3, 2)
        for slot in range(3):
            for index in (1, 2):
                admission.put_nowait(request(slot, index))
        with self.assertRaises(queue.Full):
            admission.put_nowait(request(0, 3))
        served = []
        for _ in range(6):
            item = admission.get_nowait()
            served.append((item.slot_id, item.request_id))
            admission.task_done()
        self.assertEqual(served, [(0, 1), (1, 1), (2, 1), (0, 2), (1, 2), (2, 2)])
        self.assertTrue(admission.empty())
        self.assertEqual(admission.snapshot()["rejected_capacity"], 1)

    def test_file_admission_waits_in_order_and_live_full_is_explicit(self):
        admission = FairRequestQueue(ThreadContext(), 1)
        admission.put(request(0, 1))
        waited = threading.Event()
        health = Mock()
        health.emit.side_effect = lambda *_a, **_kw: waited.set()
        stop = threading.Event()
        writer = threading.Thread(target=admit, args=(admission.for_slot(0), request(0, 2), stop),
                                  kwargs={"timeout": .01, "ordered": True, "health": health})
        writer.start()
        try:
            self.assertTrue(waited.wait(2))
            with self.assertRaises(AdmissionShed):
                admit(admission, request(0, 3, file=False), stop, timeout=.01, ordered=False)
            self.assertEqual(admission.get(timeout=1).request_id, 1)
            admission.task_done()
            writer.join(2)
            self.assertFalse(writer.is_alive())
            self.assertEqual(admission.get(timeout=1).request_id, 2)
            admission.task_done()
            self.assertGreater(admission.snapshot()["deferred_file"], 0)
            self.assertEqual(admission.snapshot()["dropped_live"], 1)
        finally:
            stop.set()
            writer.join(2)

    def test_stale_live_returns_outcome_file_runs_and_old_generation_is_discarded(self):
        admission = FairRequestQueue(ThreadContext(), 3, 2)
        live = request(0, file=False)
        ordered = request(1)
        old = request(2, generation=1)
        self.assertTrue(live_request_stale(live))
        self.assertFalse(live_request_stale(ordered))
        for item in (live, ordered, old):
            admission.put(item)
        admission.put(None)
        adapter = Mock()
        adapter.detect_persons.return_value = []
        results = queue.Queue()
        run_person_inference_loop(
            {"models": {"yolo_config": "unused", "yolo_weights": "unused"}, "runtime": {}},
            admission, results, queue.Queue(), threading.Event(),
            adapter_factory=lambda *_args: adapter, slot_generations=[1, 1, 2],
        )
        adapter.detect_persons.assert_called_once_with(ordered.frame)
        outcomes = [results.get_nowait(), results.get_nowait()]
        self.assertEqual([(row.slot_id, row.outcome) for row in outcomes],
                         [(0, "dropped_live"), (1, "accepted")])
        self.assertTrue(results.empty())
        self.assertTrue(admission.empty())
        self.assertEqual(admission.snapshot()["stale_generation"], 1)

    def test_many_camera_batching_is_bounded_and_inflight_work_is_not_empty(self):
        admission = FairRequestQueue(ThreadContext(), 300)
        for slot in range(300):
            admission.put(request(slot))
        slots = []
        while len(slots) < 300:
            batch = collect_request_batch(admission, admission.get_nowait(), enabled=True,
                                          batch_size=8, max_wait_ms=1)
            self.assertLessEqual(len(batch.requests), 8)
            slots.extend(item.slot_id for item in batch.requests)
            self.assertFalse(admission.empty())
            for _ in batch.requests:
                admission.task_done()
        self.assertEqual(slots, list(range(300)))
        self.assertEqual(admission.snapshot()["capacity"], 300)
        self.assertEqual(admission.snapshot()["outstanding"], 0)
        self.assertEqual(admission.snapshot()["dispatches"], 300)

    def test_spawn_transport_preserves_identity_and_drains_before_sentinel(self):
        ctx = mp.get_context("spawn")
        admission = FairRequestQueue(ctx, 3, 2)
        result = ctx.Queue()
        for slot in range(3):
            admission.for_slot(slot).put(request(slot, 7, generation=4))
        admission.put(None)
        process = ctx.Process(target=consume_spawned, args=(admission, result))
        process.start()
        try:
            self.assertEqual(sorted(result.get(timeout=10)), [(0, 4, 7), (1, 4, 7), (2, 4, 7)])
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(admission.empty())
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            admission.close()
            admission.join_thread()
            result.close()
            result.join_thread()

    def test_busy_camera_is_degraded_but_dead_shared_worker_still_fails(self):
        registry = HealthRegistry(monotonic=lambda: 100)
        registry.register_worker("person")
        registry.register_camera("A", 0, 1, "RUNNING")
        registry.handle(HealthEvent("camera_ingest", "camera", "frame_progress", 100,
                                   camera_id="A", slot_id=0, generation=1, progress=1))
        registry.handle(HealthEvent("person", "shared_worker", "heartbeat", 300))
        registry.handle(HealthEvent("camera_worker", "camera", "capacity_wait", 300,
                                   camera_id="A", slot_id=0, generation=1, detail="person"))
        actions, _ = registry.evaluate(HealthPolicy(), now=300)
        self.assertEqual(actions, [])
        self.assertEqual(registry.cameras["A"]["reason"], "queue_pressure")
        # Ingest/preview may exit cleanly at file EOF before inference drains.
        registry.handle(HealthEvent("camera_ingest", "camera", "eof", 300,
                                   camera_id="A", slot_id=0, generation=1))
        actions, _ = registry.evaluate(
            HealthPolicy(), now=300,
            camera_process_alive={"A": {"ingest": False, "preview": False, "camera": True}},
        )
        self.assertEqual(actions, [])
        registry.evaluate(HealthPolicy(), now=300, worker_process_alive={"person": False})
        self.assertEqual(registry.runtime_health, "FAILED")


if __name__ == "__main__":
    unittest.main()
