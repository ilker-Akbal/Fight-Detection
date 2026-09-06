from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path

from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager
from fight.pipeline_mp.health import (
    DEGRADED,
    EOF,
    FAILED,
    HEALTHY,
    ONLINE,
    RECONNECTING,
    STOPPED,
    STOPPING,
    HealthEmitter,
    HealthPolicy,
    HealthRegistry,
    HealthSnapshotStore,
    RuntimeWatchdog,
)
from fight.pipeline_mp.messages import HealthEvent


class FakeClock:
    def __init__(self, value: float = 100.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


class FakeProcess:
    next_pid = 61000

    def __init__(self, name: str):
        self.name = name
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.alive = True
        self.exitcode = None

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        return None

    def terminate(self):
        self.alive = False
        self.exitcode = -15

    def kill(self):
        self.terminate()


class FakeContext:
    Event = threading.Event

    @staticmethod
    def Queue(maxsize=0):
        return queue.Queue(maxsize=maxsize)


def policy(**overrides) -> HealthPolicy:
    values = {
        "startup_grace_sec": 2.0,
        "camera_heartbeat_timeout_sec": 3.0,
        "camera_frame_stall_warn_sec": 4.0,
        "camera_frame_stall_fail_sec": 8.0,
        "camera_reconnect_grace_sec": 6.0,
        "shared_worker_heartbeat_timeout_sec": 6.0,
        "inference_stall_warn_sec": 4.0,
        "inference_stall_fail_sec": 8.0,
        "preview_heartbeat_timeout_sec": 5.0,
        "watchdog_camera_restart_cooldown_sec": 10.0,
        "watchdog_camera_restart_limit": 2,
    }
    values.update(overrides)
    return HealthPolicy(**values)


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.registry = HealthRegistry(monotonic=self.clock, transition_limit=8)

    def event(
        self,
        component: str,
        event_type: str,
        *,
        camera_id: str = "",
        slot_id: int = -1,
        generation: int = -1,
        progress: int = 0,
        secondary_progress: int = 0,
        detail: str = "",
    ) -> HealthEvent:
        return HealthEvent(
            component=component,
            component_type="camera" if camera_id else "shared_worker",
            event_type=event_type,
            monotonic_ts=self.clock(),
            camera_id=camera_id,
            slot_id=slot_id,
            generation=generation,
            progress=progress,
            secondary_progress=secondary_progress,
            detail=detail,
        )

    def register_online_camera(self, camera_id="A", slot_id=0, generation=1):
        self.registry.register_camera(camera_id, slot_id, generation, "RUNNING")
        for component, event_type in (
            ("camera_ingest", "frame_progress"),
            ("camera_worker", "frame_consumed"),
            ("camera_preview", "preview_published"),
        ):
            self.assertTrue(
                self.registry.handle(
                    self.event(
                        component,
                        event_type,
                        camera_id=camera_id,
                        slot_id=slot_id,
                        generation=generation,
                        progress=1,
                        secondary_progress=1,
                    )
                )
            )

    def evaluate(self, camera_alive=None, worker_alive=None):
        return self.registry.evaluate(
            policy(),
            now=self.clock(),
            camera_process_alive=camera_alive or {},
            worker_process_alive=worker_alive or {},
        )

    def test_camera_progress_warn_and_fail_are_monotonic_and_deterministic(self):
        self.register_online_camera()
        self.evaluate()
        self.assertEqual(self.registry.cameras["A"]["health"], ONLINE)

        self.clock.advance(5)
        for component in ("camera_ingest", "camera_worker", "camera_preview"):
            self.registry.handle(
                self.event(
                    component,
                    "preview_published" if component == "camera_preview" else "heartbeat",
                    camera_id="A",
                    slot_id=0,
                    generation=1,
                )
            )
        actions, _ = self.evaluate()
        self.assertEqual(actions, [])
        self.assertEqual(self.registry.cameras["A"]["health"], DEGRADED)
        self.assertEqual(self.registry.cameras["A"]["reason"], "frame_stall")

        self.clock.advance(4)
        for component in ("camera_ingest", "camera_worker", "camera_preview"):
            self.registry.handle(
                self.event(
                    component,
                    "preview_published" if component == "camera_preview" else "heartbeat",
                    camera_id="A",
                    slot_id=0,
                    generation=1,
                )
            )
        actions, _ = self.evaluate()
        self.assertEqual(self.registry.cameras["A"]["health"], FAILED)
        self.assertEqual(actions[0]["action"], "restart_camera")

    def test_stopping_stopped_eof_and_reconnecting_never_request_restart(self):
        for lifecycle, expected in (("STOPPING", STOPPING), ("STOPPED", STOPPED)):
            registry = HealthRegistry(monotonic=self.clock)
            registry.register_camera(lifecycle, 0, 1, lifecycle)
            actions, _ = registry.evaluate(policy(), now=self.clock())
            self.assertEqual(actions, [])
            self.assertEqual(registry.cameras[lifecycle]["health"], expected)

        self.registry.sync_cameras(
            {"file": {"slot_id": 0, "generation": 1, "state": "STOPPED", "file_done": True}}
        )
        actions, _ = self.evaluate()
        self.assertEqual(actions, [])
        self.assertEqual(self.registry.cameras["file"]["health"], EOF)

        self.registry.sync_cameras(
            {"live": {"slot_id": 0, "generation": 2, "state": "RUNNING"}}
        )
        self.registry.handle(
            self.event("camera_ingest", "reconnecting", camera_id="live", slot_id=0, generation=2)
        )
        self.clock.advance(5)
        actions, _ = self.evaluate()
        self.assertEqual(actions, [])
        self.assertEqual(self.registry.cameras["live"]["health"], RECONNECTING)

    def test_generation_and_slot_reuse_reject_old_health(self):
        self.register_online_camera("old", 0, 3)
        self.registry.sync_cameras(
            {"new": {"slot_id": 0, "generation": 4, "state": "RUNNING"}}
        )
        stale = self.event(
            "camera_ingest", "process_error", camera_id="old", slot_id=0, generation=3
        )
        wrong_generation = self.event(
            "camera_ingest", "process_error", camera_id="new", slot_id=0, generation=3
        )
        self.assertFalse(self.registry.handle(stale))
        self.assertFalse(self.registry.handle(wrong_generation))
        self.assertNotIn("old", self.registry.cameras)
        self.assertEqual(
            self.registry.cameras["new"]["components"]["camera_ingest"]["source_state"],
            "STARTING",
        )

    def test_shared_idle_heartbeat_and_inference_stall_policy(self):
        self.registry.register_worker("person")
        self.registry.handle(self.event("person", "heartbeat"))
        self.clock.advance(3)
        self.registry.handle(self.event("person", "heartbeat"))
        self.evaluate(worker_alive={"person": True})
        self.assertEqual(self.registry.workers["person"]["health"], HEALTHY)

        self.registry.handle(self.event("person", "request_received", progress=1))
        self.clock.advance(5)
        self.registry.handle(self.event("person", "heartbeat"))
        self.evaluate(worker_alive={"person": True})
        self.assertEqual(self.registry.workers["person"]["health"], DEGRADED)
        self.assertEqual(self.registry.workers["person"]["reason"], "inference_stall")
        self.clock.advance(4)
        self.registry.handle(self.event("person", "heartbeat"))
        self.evaluate(worker_alive={"person": True})
        self.assertEqual(self.registry.workers["person"]["health"], FAILED)
        self.assertEqual(self.registry.runtime_health, FAILED)

    def test_stale_or_dead_shared_worker_is_runtime_fatal(self):
        self.registry.register_worker("stage3")
        self.clock.advance(7)
        self.evaluate(worker_alive={"stage3": True})
        self.assertEqual(self.registry.workers["stage3"]["reason"], "heartbeat_timeout")
        self.assertEqual(self.registry.runtime_health, FAILED)

        registry = HealthRegistry(monotonic=self.clock)
        registry.register_worker("incident")
        registry.evaluate(policy(), now=self.clock(), worker_process_alive={"incident": False})
        self.assertEqual(registry.runtime_health, FAILED)

    def test_inflight_inference_uses_warn_and_fail_not_loop_heartbeat(self):
        # Exercise the vulnerable ordering: heartbeat timeout < inference warn.
        thresholds = policy(shared_worker_heartbeat_timeout_sec=2)
        for component in ("person", "pose", "stage3"):
            with self.subTest(component=component):
                registry = HealthRegistry(monotonic=self.clock)
                registry.register_worker(component)
                registry.handle(self.event(component, "request_received", progress=1))
                for elapsed, expected, reason in (
                    (3, HEALTHY, "inference_in_progress"),
                    (4, DEGRADED, "inference_stall"),
                    (8, FAILED, "inference_stall"),
                ):
                    registry.evaluate(thresholds, now=self.clock() + elapsed)
                    self.assertEqual(registry.workers[component]["health"], expected)
                    self.assertEqual(registry.workers[component]["reason"], reason)

    def test_lazy_first_inference_grace_is_bounded_and_never_masks_death(self):
        thresholds = policy(startup_grace_sec=20)
        for component in ("person", "pose", "stage3"):
            with self.subTest(component=component):
                registry = HealthRegistry(monotonic=self.clock)
                registry.register_worker(component)
                # First request can arrive after the process startup grace ended.
                self.clock.advance(30)
                registry.handle(self.event(component, "request_received", progress=1))
                registry.evaluate(thresholds, now=self.clock() + 9)
                self.assertEqual(registry.workers[component]["health"], DEGRADED)
                registry.evaluate(
                    thresholds, now=self.clock() + 9,
                    worker_process_alive={component: False},
                )
                self.assertEqual(registry.workers[component]["reason"], "process_dead")
                registry.evaluate(thresholds, now=self.clock() + 20)
                self.assertEqual(registry.workers[component]["health"], FAILED)
                self.assertEqual(registry.workers[component]["reason"], "inference_stall")

    def test_completion_or_result_restores_idle_policy_and_ends_warmup_grace(self):
        thresholds = policy(startup_grace_sec=20)
        for finished_event in ("inference_completed", "result_delivered", "result_produced"):
            with self.subTest(finished_event=finished_event):
                registry = HealthRegistry(monotonic=self.clock)
                registry.register_worker("pose")
                self.clock.advance(30)
                registry.handle(self.event("pose", "request_received", progress=1))
                self.clock.advance(9)
                registry.handle(self.event("pose", finished_event, progress=1))
                registry.evaluate(thresholds)
                self.assertEqual(registry.workers["pose"]["health"], HEALTHY)
                registry.evaluate(thresholds, now=self.clock() + 6)
                self.assertEqual(registry.workers["pose"]["reason"], "heartbeat_timeout")
                self.clock.advance(1)
                registry.handle(self.event("pose", "request_received", progress=2))
                registry.evaluate(thresholds, now=self.clock() + 8)
                self.assertEqual(registry.workers["pose"]["health"], FAILED)
                self.assertEqual(registry.workers["pose"]["reason"], "inference_stall")

    def test_shared_inference_wait_suppresses_only_dependent_camera_stall(self):
        thresholds = policy(inference_stall_fail_sec=20)
        self.register_online_camera("waiting", 0)
        self.register_online_camera("unrelated", 1)
        self.registry.register_worker("pose")
        self.clock.advance(0.1)
        self.registry.handle(self.event(
            "camera_worker", "pose_request", camera_id="waiting", slot_id=0,
            generation=1,
        ))
        self.registry.handle(self.event("pose", "request_received", progress=1))
        self.clock.advance(9)
        actions, _ = self.registry.evaluate(thresholds)
        self.assertEqual(self.registry.workers["pose"]["health"], DEGRADED)
        self.assertEqual(self.registry.cameras["waiting"]["reason"], "shared_inference_in_progress")
        self.assertEqual([action["camera_id"] for action in actions], ["unrelated"])
        # Genuine process death is still actionable during shared warm-up.
        actions, _ = self.registry.evaluate(
            thresholds, camera_process_alive={"waiting": {"camera": False}}
        )
        self.assertIn("waiting", [action["camera_id"] for action in actions])
        # The shared deadline remains fatal and lifts the camera exemption.
        self.registry.evaluate(thresholds, now=self.clock() + 11)
        self.assertEqual(self.registry.runtime_health, FAILED)
        self.assertEqual(self.registry.cameras["waiting"]["reason"], "frame_stall")
        # A completed request also removes the exemption immediately.
        self.registry.handle(self.event("pose", "inference_completed", progress=1))
        actions, _ = self.registry.evaluate(thresholds)
        self.assertIn("waiting", [action["camera_id"] for action in actions])

    def test_preview_failure_and_single_camera_failure_only_degrade_runtime(self):
        self.registry.register_worker("person")
        self.registry.handle(self.event("person", "heartbeat"))
        self.register_online_camera()
        alive = {"A": {"ingest": True, "camera": True, "preview": False}}
        actions, _ = self.evaluate(alive, {"person": True})
        self.assertEqual(actions[0]["action"], "restart_preview")
        self.assertEqual(self.registry.runtime_health, DEGRADED)

        alive["A"]["preview"] = True
        alive["A"]["camera"] = False
        actions, _ = self.evaluate(alive, {"person": True})
        self.assertEqual(actions[0]["action"], "restart_camera")
        self.assertEqual(self.registry.cameras["A"]["health"], FAILED)
        self.assertEqual(self.registry.runtime_health, DEGRADED)

    def test_preview_publish_stall_is_noncritical_even_with_fresh_heartbeat(self):
        self.register_online_camera()
        self.clock.advance(6)
        self.registry.handle(
            self.event("camera_ingest", "frame_progress", camera_id="A", slot_id=0, generation=1)
        )
        self.registry.handle(
            self.event("camera_worker", "frame_consumed", camera_id="A", slot_id=0, generation=1)
        )
        self.registry.handle(
            self.event("camera_preview", "heartbeat", camera_id="A", slot_id=0, generation=1)
        )
        actions, _ = self.evaluate()
        self.assertEqual(actions[0]["action"], "restart_preview")
        self.assertEqual(self.registry.cameras["A"]["health"], DEGRADED)
        self.assertEqual(self.registry.runtime_health, DEGRADED)

    def test_emitter_is_throttled_bounded_and_has_no_arbitrary_payload(self):
        channel = queue.Queue(maxsize=1)
        emitter = HealthEmitter(
            channel,
            component="person",
            component_type="shared_worker",
            interval_sec=1,
            monotonic=self.clock,
        )
        self.assertTrue(emitter.heartbeat(progress=1))
        self.assertFalse(emitter.heartbeat(progress=2))
        self.clock.advance(2)
        self.assertFalse(emitter.heartbeat(progress=3))
        event = channel.get_nowait()
        self.assertIsInstance(event, HealthEvent)
        self.assertFalse(hasattr(event, "payload"))

    def test_registry_history_and_camera_population_stay_bounded(self):
        registry = HealthRegistry(monotonic=self.clock, transition_limit=4, max_cameras=300)
        for index in range(350):
            registry.register_camera(f"cam-{index}", index, 1, "RUNNING")
        self.assertEqual(len(registry.cameras), 300)
        for _ in range(50):
            for index in range(10):
                registry.handle(
                    HealthEvent(
                        "camera_ingest",
                        "camera",
                        "heartbeat",
                        monotonic_ts=self.clock(),
                        camera_id=f"cam-{index}",
                        slot_id=index,
                        generation=1,
                    )
                )
            registry.evaluate(policy(), now=self.clock())
            self.clock.advance(0.1)
        self.assertLessEqual(len(registry.transitions), 4)
        registry.sync_cameras({})
        self.assertEqual(registry.cameras, {})

    def test_snapshot_is_atomic_compact_bounded_and_credential_free(self):
        registry = HealthRegistry(monotonic=self.clock, max_cameras=2)
        registry.sync_cameras(
            {
                "A": {
                    "slot_id": 0,
                    "generation": 1,
                    "state": "RUNNING",
                    "source": "rtsp://user:password@host/live",
                },
                "B": {"slot_id": 1, "generation": 1, "state": "RUNNING"},
            }
        )
        registry.register_camera("C", 2, 1, "RUNNING")
        registry.register_worker("person")
        snapshot = registry.snapshot("run-1", now=self.clock())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime_health.json"
            HealthSnapshotStore(path).write(snapshot)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(path.with_suffix(".json.tmp").exists())
        self.assertEqual(loaded["camera_count"], 2)
        self.assertEqual(loaded["worker_count"], 1)
        serialized = json.dumps(loaded)
        self.assertNotIn("password", serialized)
        self.assertNotIn("rtsp://", serialized)


class RuntimeWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.processes = []

        def process_factory(name, _target, _args):
            process = FakeProcess(name)
            self.processes.append(process)
            return process

        self.manager = CameraRuntimeManager(
            ctx=FakeContext(),
            config={
                "output_dir": ".",
                "runtime": {
                    "camera_ingest_mode": "centralized",
                    "camera_restart_backoff_sec": 0,
                },
            },
            stage3_queue=queue.Queue(),
            report_queue=queue.Queue(),
            person_request_queue=queue.Queue(),
            person_result_channels={0: queue.Queue()},
            pose_request_queue=None,
            pose_result_channels={0: queue.Queue()},
            slot_generations=[0],
            health_queue=queue.Queue(),
            process_factory=process_factory,
            terminate_process=lambda process, timeout=0: process.terminate(),
            close_queue=lambda _channel: None,
        )
        self.manager.reconcile(
            [
                {
                    "camera_id": "A",
                    "source": "rtsp://host/A",
                    "enabled": True,
                    "use_fight_detection": True,
                }
            ]
        )
        self.registry = HealthRegistry(monotonic=self.clock)
        self.watchdog = RuntimeWatchdog(
            self.registry,
            policy(),
            queue.Queue(),
            monotonic=self.clock,
        )

    def test_camera_restart_uses_manager_generation_and_cooldown(self):
        old = self.manager.runtimes["A"]
        old.processes["camera"].alive = False
        self.watchdog.tick(self.manager, {})
        restarted = self.manager.runtimes["A"]
        self.assertEqual(restarted.generation, old.generation + 1)
        self.assertEqual(len(self.processes), 6)

        restarted.processes["camera"].alive = False
        self.clock.advance(1)
        self.watchdog.tick(self.manager, {})
        self.assertIs(self.manager.runtimes["A"], restarted)
        self.assertEqual(len(self.processes), 6)

    def test_preview_restart_is_isolated_and_camera_removal_cleans_watchdog_state(self):
        runtime = self.manager.runtimes["A"]
        generation = runtime.generation
        runtime.processes["preview"].alive = False
        self.watchdog.tick(self.manager, {})
        self.assertEqual(runtime.generation, generation)
        self.assertEqual(len(self.processes), 4)
        self.assertTrue(runtime.processes["camera"].is_alive())

        self.manager.reconcile([])
        self.clock.advance(20)
        self.assertFalse(self.watchdog.tick(self.manager, {}))
        self.assertNotIn("A", self.registry.cameras)
        self.assertEqual(self.manager.runtimes, {})

    def test_watchdog_restart_limit_settles_camera_failed(self):
        watchdog = RuntimeWatchdog(
            self.registry,
            policy(
                watchdog_camera_restart_cooldown_sec=0.1,
                watchdog_camera_restart_limit=1,
            ),
            queue.Queue(),
            monotonic=self.clock,
        )
        self.manager.runtimes["A"].processes["camera"].alive = False
        watchdog.tick(self.manager, {})
        restarted = self.manager.runtimes["A"]
        restarted.processes["camera"].alive = False
        self.clock.advance(1)
        watchdog.tick(self.manager, {})
        self.assertIs(self.manager.runtimes["A"], restarted)
        self.assertEqual(restarted.state, FAILED)
        self.assertEqual(len(self.processes), 6)

    def test_critical_shared_failure_requests_controlled_runtime_failure(self):
        self.registry.register_worker("person")
        dead = FakeProcess("person")
        dead.alive = False
        self.assertTrue(self.watchdog.tick(self.manager, {"person": dead}))


if __name__ == "__main__":
    unittest.main()
