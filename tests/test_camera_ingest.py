from __future__ import annotations

import pickle
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from fight.pipeline_mp.camera_ingest import publish_latest, run_camera_ingest_loop
from fight.pipeline_mp.camera_preview import run_preview_consumer_loop
from fight.pipeline_mp.camera_worker import CameraProcessRunner
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal, ReportMessage
from fight.pipeline_mp.performance import BoundedMetricCollector


class FakeCapture:
    def __init__(self, frames, *, stop_event=None, stop_after_frame=None, opened=True):
        self.frames = list(frames)
        self.stop_event = stop_event
        self.stop_after_frame = stop_after_frame
        self.opened = opened
        self.read_count = 0
        self.release_count = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.read_count += 1
        if not self.frames:
            return False, None
        frame = self.frames.pop(0)
        if self.stop_event is not None and self.read_count == self.stop_after_frame:
            self.stop_event.set()
        return True, frame

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return 25.0
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return 12.0
        return 0.0

    def release(self):
        self.release_count += 1


def _drain(channel):
    items = []
    while True:
        try:
            items.append(channel.get_nowait())
        except queue.Empty:
            return items


def _status_rows(channel):
    return [item.row for item in _drain(channel) if isinstance(item, ReportMessage)]


class CameraIngestTests(unittest.TestCase):
    def test_file_source_opens_once_and_fans_out_identity_then_eof(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "camera.mp4"
            source.touch()
            frames = [
                np.full((4, 6, 3), 11, dtype=np.uint8),
                np.full((4, 6, 3), 22, dtype=np.uint8),
            ]
            capture = FakeCapture(frames)
            opens = []
            fight = queue.Queue(maxsize=8)
            preview = queue.Queue(maxsize=8)
            reports = queue.Queue()

            def factory(value):
                opens.append(value)
                return capture

            run_camera_ingest_loop(
                {"runtime": {"camera_ingest_file_fight_policy": "ordered"}},
                {"camera_id": "cam_A", "source": str(source)},
                fight,
                preview,
                reports,
                threading.Event(),
                7,
                capture_factory=factory,
            )

            self.assertEqual(opens, [str(source)])
            self.assertEqual(capture.release_count, 1)
            fight_items = _drain(fight)
            preview_items = _drain(preview)
            fight_frames = [item for item in fight_items if isinstance(item, CameraFrame)]
            preview_frames = [item for item in preview_items if isinstance(item, CameraFrame)]
            self.assertEqual([item.frame_seq for item in fight_frames], [1, 2])
            self.assertEqual([item.frame_seq for item in preview_frames], [1, 2])
            self.assertEqual([item.generation for item in fight_frames], [7, 7])
            self.assertEqual(
                [(item.camera_id, item.generation, item.frame_seq) for item in fight_frames],
                [(item.camera_id, item.generation, item.frame_seq) for item in preview_frames],
            )
            self.assertEqual(len([x for x in fight_items if isinstance(x, CameraIngestSignal)]), 1)
            self.assertEqual(len([x for x in preview_items if isinstance(x, CameraIngestSignal)]), 1)
            details = [row["detail"] for row in _status_rows(reports)]
            self.assertIn("eof", details)
            self.assertNotIn("reconnecting", details)

    def test_latest_channels_are_bounded_and_slow_consumers_do_not_block(self):
        channel = queue.Queue(maxsize=2)
        for seq in range(1, 10):
            published, _ = publish_latest(channel, seq)
            self.assertTrue(published)
        self.assertEqual(_drain(channel), [8, 9])

        stop_event = threading.Event()
        frames = [np.zeros((2, 2, 3), dtype=np.uint8) for _ in range(10)]
        capture = FakeCapture(frames, stop_event=stop_event, stop_after_frame=10)
        fight = queue.Queue(maxsize=1)
        preview = queue.Queue(maxsize=1)
        reports = queue.Queue()
        started = time.perf_counter()
        run_camera_ingest_loop(
            {"runtime": {"camera_reconnect_enabled": True}},
            {"camera_id": "cam_live", "source": "rtsp://user:secret@example.test/live"},
            fight,
            preview,
            reports,
            stop_event,
            1,
            capture_factory=lambda _: capture,
            sleep_fn=lambda _: None,
        )
        self.assertLess(time.perf_counter() - started, 1.0)
        rows = _status_rows(reports)
        summary = next(row for row in rows if row["detail"] == "summary")
        self.assertEqual(summary["frames_decoded"], 10)
        self.assertEqual(summary["frames_dropped_fight"], 9)
        self.assertEqual(summary["frames_dropped_preview"], 9)
        self.assertNotIn("user", repr(rows))
        self.assertNotIn("secret", repr(rows))

    def test_live_read_failure_reconnects_with_bounded_policy(self):
        stop_event = threading.Event()
        first = FakeCapture([np.zeros((2, 2, 3), dtype=np.uint8)])
        second = FakeCapture(
            [np.ones((2, 2, 3), dtype=np.uint8)],
            stop_event=stop_event,
            stop_after_frame=1,
        )
        captures = iter([first, second])
        opens = []
        reports = queue.Queue()

        def factory(source):
            opens.append(source)
            return next(captures)

        run_camera_ingest_loop(
            {
                "runtime": {
                    "camera_reconnect_enabled": True,
                    "camera_reconnect_initial_delay_sec": 0.01,
                    "camera_reconnect_max_delay_sec": 0.02,
                }
            },
            {"camera_id": "cam_live", "source": "rtsp://host/live"},
            queue.Queue(maxsize=4),
            queue.Queue(maxsize=4),
            reports,
            stop_event,
            3,
            capture_factory=factory,
            sleep_fn=lambda _: None,
        )
        rows = _status_rows(reports)
        details = [row["detail"] for row in rows]
        self.assertEqual(len(opens), 2)
        self.assertEqual(first.release_count, 1)
        self.assertEqual(second.release_count, 1)
        self.assertIn("read_failed", details)
        self.assertIn("reconnecting", details)
        self.assertIn("reconnected", details)
        summary = next(row for row in rows if row["detail"] == "summary")
        self.assertEqual(summary["reconnect_count"], 1)

    def test_camera_frame_is_pickle_safe(self):
        original = CameraFrame(
            camera_id="cam_pickle",
            generation=4,
            frame_seq=9,
            captured_monotonic=1.25,
            captured_wall_time=2.5,
            frame=np.zeros((2, 3, 3), dtype=np.uint8),
        )
        restored = pickle.loads(pickle.dumps(original))
        self.assertEqual((restored.camera_id, restored.generation, restored.frame_seq), ("cam_pickle", 4, 9))
        np.testing.assert_array_equal(restored.frame, original.frame)

    def test_preview_consumer_writes_ingest_frame_without_source_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            channel = queue.Queue()
            channel.put(
                CameraFrame(
                    camera_id="cam_preview",
                    generation=2,
                    frame_seq=1,
                    captured_monotonic=time.perf_counter(),
                    captured_wall_time=time.time(),
                    frame=np.full((5, 7, 3), 127, dtype=np.uint8),
                )
            )
            channel.put(CameraIngestSignal("cam_preview", 2, "eof", frame_seq=1))
            run_preview_consumer_loop(
                {
                    "output_dir": temp_dir,
                    "runtime": {"preview_write_interval_sec": 0.0},
                },
                {"camera_id": "cam_preview", "source": "rtsp://host/live"},
                channel,
                queue.Queue(),
                threading.Event(),
                2,
            )
            preview = Path(temp_dir) / "previews" / "cam_preview.jpg"
            self.assertTrue(preview.is_file())
            self.assertIsNotNone(cv2.imread(str(preview)))

    def test_centralized_camera_runner_never_opens_capture(self):
        runner = CameraProcessRunner.__new__(CameraProcessRunner)
        runner.centralized_ingest = True
        runner.frame_queue = queue.Queue()
        runner.frame_queue.put(
            CameraFrame(
                camera_id="cam_A",
                generation=5,
                frame_seq=1,
                captured_monotonic=time.perf_counter(),
                captured_wall_time=time.time(),
                frame=np.zeros((2, 2, 3), dtype=np.uint8),
                source_fps=25.0,
                source_frame_count=1,
            )
        )
        runner.frame_queue.put(CameraIngestSignal("cam_A", 5, "eof", frame_seq=1))
        runner.stop_event = threading.Event()
        runner.camera_id = "cam_A"
        runner.generation = 5
        runner.active_event = None
        runner.last_ingest_frame_seq = 0
        runner.capture_fps = 16.0
        runner.clip_write_fps = 16.0
        runner.source_frame_count = 0
        runner.source_timeline_base_ts = 0.0
        runner.source_is_file = True
        runner.frame_idx = -1
        runner.counters = {"frames_read": 0}
        runner.frame_age_ms = BoundedMetricCollector(enabled=True)
        runner.motion = SimpleNamespace(close=Mock())
        runner.report_status = Mock()
        runner.process_frame = Mock()
        runner.close_event = Mock()

        with patch("fight.pipeline_mp.camera_worker.open_source") as open_source:
            runner.run_loop()

        open_source.assert_not_called()
        runner.process_frame.assert_called_once()
        self.assertEqual(runner.last_ingest_frame_seq, 1)
        self.assertEqual(runner.counters["frames_read"], 1)


if __name__ == "__main__":
    unittest.main()
