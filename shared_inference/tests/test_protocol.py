from __future__ import annotations

import queue
import threading
import time
import unittest

from shared_inference.protocol import InferenceClient, InferenceJob, InferenceResult, RouteCommand
from shared_inference.runtime import inference_worker_main, result_router_main


def job(pipeline="fight", stage="person_detection", camera="cam1", generation="g1", frame=1):
    return InferenceJob.create(pipeline=pipeline, stage=stage, camera_id=camera,
                               session_id="s1", generation_id=generation,
                               frame_id=frame, timestamp_ns=time.time_ns(), image=frame)


class ProtocolTests(unittest.TestCase):
    def test_result_preserves_all_job_identity(self):
        item = job(frame=7)
        result = InferenceResult.from_job(item, success=True, payload=[1])
        for name in ("pipeline", "stage", "camera_id", "session_id", "generation_id",
                     "frame_id", "timestamp_ns", "request_id"):
            self.assertEqual(getattr(result, name), getattr(item, name))

    def test_router_isolates_pipeline_camera_and_generation(self):
        results, commands = queue.Queue(), queue.Queue()
        fight_out, speed_out = queue.Queue(maxsize=2), queue.Queue(maxsize=2)
        stop = threading.Event()
        routes = {("fight", "same"): fight_out, ("speed", "same"): speed_out}
        thread = threading.Thread(target=result_router_main,
                                  args=(results, commands, stop, routes), daemon=True)
        thread.start()
        commands.put(RouteCommand("register", "fight", "same", "s1", "g2"))
        commands.put(RouteCommand("register", "speed", "same", "s1", "g1"))
        stale = job(camera="same", generation="g1")
        valid = job(pipeline="speed", stage="vehicle_detection", camera="same", generation="g1")
        results.put(InferenceResult.from_job(stale, success=True))
        results.put(InferenceResult.from_job(valid, success=True, payload="speed"))
        self.assertEqual(speed_out.get(timeout=1).payload, "speed")
        self.assertTrue(fight_out.empty())
        commands.put(RouteCommand("unregister", "speed", "same", "s1", "g1"))
        results.put(InferenceResult.from_job(valid, success=True))
        time.sleep(.15)
        self.assertTrue(speed_out.empty())
        results.put(None)
        thread.join(1)

    def test_camera_rejects_unknown_duplicate_and_out_of_order(self):
        client = InferenceClient(pipeline="fight", camera_id="c", session_id="s1",
                                 generation_id="g1", result_channel=queue.Queue(),
                                 job_queues={}, timeout_sec=.1)
        newer = job(camera="c", frame=102)
        client.pending[newer.request_id] = (newer.stage, newer.frame_id, newer.timestamp_ns, time.monotonic())
        self.assertIsNotNone(client._accept(InferenceResult.from_job(newer, success=True, payload=[]), expected_stage=newer.stage))
        older = job(camera="c", frame=101)
        client.pending[older.request_id] = (older.stage, older.frame_id, older.timestamp_ns, time.monotonic())
        self.assertIsNone(client._accept(InferenceResult.from_job(older, success=True, payload=[]), expected_stage=older.stage))
        self.assertIsNone(client._accept(InferenceResult.from_job(newer, success=True), expected_stage=newer.stage))
        self.assertEqual(client.metrics["stale_results"], 1)
        self.assertEqual(client.metrics["unknown_request"], 1)

    def test_camera_rejects_old_session_and_expires_pending(self):
        client = InferenceClient(pipeline="speed", camera_id="c", session_id="new",
                                 generation_id="g2", result_channel=queue.Queue(),
                                 job_queues={}, timeout_sec=.05)
        old = InferenceJob.create(pipeline="speed", stage="vehicle_detection",
                                  camera_id="c", session_id="old", generation_id="g1",
                                  frame_id=1, timestamp_ns=time.time_ns(), image=None)
        client.pending[old.request_id] = (old.stage, old.frame_id, old.timestamp_ns, time.monotonic())
        self.assertIsNone(client._accept(InferenceResult.from_job(old, success=True),
                                         expected_stage=old.stage))
        self.assertEqual(client.metrics["session_mismatch"], 1)
        timed = job(camera="c", frame=2)
        client.pending[timed.request_id] = (timed.stage, timed.frame_id, timed.timestamp_ns,
                                            time.monotonic() - 1)
        client.expire()
        self.assertNotIn(timed.request_id, client.pending)
        self.assertEqual(client.metrics["timeouts"], 1)

    def test_batch_worker_keeps_job_mapping_and_masks_errors(self):
        jobs, results = queue.Queue(), queue.Queue()
        stop = threading.Event()
        def handler(value):
            if value == 2:
                raise RuntimeError("secret model error")
            return value * 10
        thread = threading.Thread(target=inference_worker_main, kwargs={
            "stage": "person_detection", "job_queue": jobs, "result_queue": results,
            "stop_event": stop, "build_handler": lambda: handler,
            "max_batch_size": 3, "max_batch_wait_ms": 20,
        }, daemon=True)
        thread.start()
        submitted = [job(frame=i) for i in (1, 2, 3)]
        for item in submitted:
            item.image = item.frame_id
            jobs.put(item)
        returned = [results.get(timeout=1) for _ in submitted]
        self.assertEqual([x.request_id for x in returned], [x.request_id for x in submitted])
        self.assertEqual(returned[0].payload, 10)
        self.assertEqual(returned[1].error_code, "INFERENCE_FAILED")
        self.assertNotIn("secret", returned[1].error_code)
        jobs.put(None)
        thread.join(1)


if __name__ == "__main__":
    unittest.main()
