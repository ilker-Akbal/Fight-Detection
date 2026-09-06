from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from pathlib import Path

from fight.pipeline_mp.camera_lifecycle import CameraRuntimeManager, RUNNING, STOPPED
from fight.pipeline_mp.generation import is_current_generation
from fight.pipeline_mp.messages import (
    PersonInferenceResult,
    PersonInferenceRequest,
    PoseInferenceResult,
    Stage3Job,
    Stage3ResultMessage,
)
from fight.pipeline_mp.person_worker import route_person_result, run_person_inference_loop
from fight.pipeline_mp.pose_worker import route_pose_result


class FakeProcess:
    next_pid = 5000

    def __init__(self, name):
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
        self.alive = False
        self.exitcode = -9


class FakeContext:
    Event = threading.Event

    @staticmethod
    def Queue(maxsize=0):
        return queue.Queue(maxsize=maxsize)


class DynamicCameraLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.processes = []

        def process_factory(name, _target, _args):
            process = FakeProcess(name)
            self.processes.append(process)
            return process

        self.person_channels = {0: queue.Queue(2), 1: queue.Queue(2), 2: queue.Queue(2)}
        self.pose_channels = {0: queue.Queue(2), 1: queue.Queue(2), 2: queue.Queue(2)}
        self.generations = [0, 0, 0]
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
            person_result_channels=self.person_channels,
            pose_request_queue=queue.Queue(),
            pose_result_channels=self.pose_channels,
            slot_generations=self.generations,
            process_factory=process_factory,
            terminate_process=lambda process, timeout=0: process.terminate(),
            close_queue=lambda _channel: None,
        )

    @staticmethod
    def camera(camera_id, source=None, name=None):
        return {
            "camera_id": camera_id,
            "source": source or f"rtsp://host/{camera_id}",
            "name": name or camera_id,
            "enabled": True,
            "use_fight_detection": True,
        }

    def test_static_desired_state_is_idempotent(self):
        desired = [self.camera("A")]
        self.manager.reconcile(desired, revision=1)
        first = self.manager.runtimes["A"]
        process_count = len(self.processes)
        summary = self.manager.reconcile(desired, revision=1)
        self.assertIs(self.manager.runtimes["A"], first)
        self.assertEqual(len(self.processes), process_count)
        self.assertEqual(summary["added"], [])

    def test_add_and_remove_do_not_restart_unchanged_camera(self):
        self.manager.reconcile([self.camera("A")])
        a = self.manager.runtimes["A"]
        a_processes = dict(a.processes)
        self.manager.reconcile([self.camera("A"), self.camera("B")])
        self.assertIs(self.manager.runtimes["A"], a)
        self.assertEqual(self.manager.runtimes["A"].processes, a_processes)
        self.assertEqual(self.manager.runtimes["B"].state, RUNNING)
        self.manager.reconcile([self.camera("A")])
        self.assertIs(self.manager.runtimes["A"], a)
        self.assertNotIn("B", self.manager.runtimes)

    def test_source_change_restarts_only_camera_and_increments_generation(self):
        self.manager.reconcile([self.camera("A"), self.camera("B")])
        old_a = self.manager.runtimes["A"]
        old_generation = old_a.generation
        b = self.manager.runtimes["B"]
        self.manager.reconcile(
            [self.camera("A", "rtsp://host/new-source"), self.camera("B")]
        )
        new_a = self.manager.runtimes["A"]
        self.assertIsNot(new_a, old_a)
        self.assertEqual(new_a.generation, old_generation + 1)
        self.assertIs(self.manager.runtimes["B"], b)

    def test_cosmetic_change_does_not_restart(self):
        self.manager.reconcile([self.camera("A", name="old")])
        item = self.manager.runtimes["A"]
        self.manager.reconcile([self.camera("A", name="new")])
        self.assertIs(self.manager.runtimes["A"], item)
        self.assertEqual(item.camera["name"], "new")

    def test_stale_slot_results_are_rejected(self):
        channels = {0: queue.Queue()}
        generations = [2]
        person = PersonInferenceResult("A", 1, 4, 8, slot_id=0)
        pose = PoseInferenceResult("A", 1, 4, 8, slot_id=0)
        self.assertEqual(
            route_person_result(person, channels, 0.1, generations),
            (False, "stale_generation"),
        )
        self.assertEqual(
            route_pose_result(pose, channels, 0.1, generations),
            (False, "stale_generation"),
        )
        person.generation = 2
        self.assertEqual(route_person_result(person, channels, 0.1, generations)[0], True)
        stage3_job = Stage3Job(
            "A", "safe-source", "event", 1.0, 2.0, 0.5, 0.4, "clip", [],
            generation=1,
            slot_id=0,
        )
        stage3_result = Stage3ResultMessage(
            "A", "safe-source", "event", 1.0, 2.0, "clip", 0.9, "fight", 0.5, 0.4,
            generation=1,
            slot_id=0,
        )
        self.assertFalse(is_current_generation(stage3_job, generations))
        self.assertFalse(is_current_generation(stage3_result, generations))

    def test_pending_stale_request_is_discarded_before_inference(self):
        class Adapter:
            calls = 0

            def detect_persons(self, _frame):
                self.calls += 1
                return []

        requests = queue.Queue()
        results = queue.Queue()
        requests.put(PersonInferenceRequest("A", 1, 1, 1, object(), slot_id=0))
        requests.put(None)
        adapter = Adapter()
        run_person_inference_loop(
            {"models": {"yolo_config": "unused", "yolo_weights": "unused"}, "runtime": {}},
            requests,
            results,
            queue.Queue(),
            threading.Event(),
            adapter_factory=lambda *_args: adapter,
            slot_generations=[2],
        )
        self.assertEqual(adapter.calls, 0)
        self.assertTrue(results.empty())

    def test_camera_crash_isolated_and_preview_restart_keeps_generation(self):
        self.manager.reconcile([self.camera("A"), self.camera("B")])
        b = self.manager.runtimes["B"]
        old_a = self.manager.runtimes["A"]
        old_a.processes["camera"].alive = False
        old_a.processes["camera"].exitcode = 17
        self.manager.poll()
        self.assertIs(self.manager.runtimes["B"], b)
        self.assertIsNot(self.manager.runtimes["A"], old_a)
        generation = b.generation
        old_preview = b.processes["preview"]
        old_preview.alive = False
        old_preview.exitcode = 11
        self.manager.poll()
        self.assertIsNot(b.processes["preview"], old_preview)
        self.assertEqual(b.generation, generation)

    def test_file_eof_is_clean_stopped_state(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "clip.mp4"
            source.touch()
            self.manager.reconcile([self.camera("file", str(source))])
            item = self.manager.runtimes["file"]
            for name in ("ingest", "camera", "preview"):
                item.processes[name].alive = False
                item.processes[name].exitcode = 0
            self.manager.poll()
            self.assertEqual(item.state, STOPPED)
            self.assertTrue(item.file_done)
            self.assertTrue(self.manager.all_file_cameras_done())

    def test_repeated_add_remove_reuses_one_slot_without_duplicates(self):
        used_slots = []
        for _ in range(4):
            self.manager.reconcile([self.camera("A")])
            used_slots.append(self.manager.runtimes["A"].slot_id)
            self.manager.reconcile([])
            self.assertEqual(self.manager.runtimes, {})
        self.assertEqual(set(used_slots), {0})
        self.assertEqual(self.manager._free_slots, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
