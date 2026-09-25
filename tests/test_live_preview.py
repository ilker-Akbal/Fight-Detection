"""Phase 24: lossy preview is not lifecycle or ordered inference truth."""
import http.client
import json
import queue
import threading
import time
from unittest.mock import patch

import numpy as np
import pytest

from fight.pipeline_mp.camera_preview import run_preview_consumer_loop
from fight.pipeline_mp.messages import CameraFrame, CameraIngestSignal
from fight.pipeline_mp.preview_gateway import PreviewCache, PreviewGateway
from fight.pipeline_mp.fight_identity import FightGenerations
from fight.pipeline_mp.generation import is_current_generation
from fight.pipeline_mp.messages import PersonInferenceResult
from fight.pipeline_mp.speed_worker import SpeedGenerationGuard
from tests.test_shared_services import fixture, reconcile, close
from tests.test_speed_integration import camera


def test_cache_latest_bounded_stale_and_generation_fenced():
    generations = [1, 2]
    now = [100.0]
    cache = PreviewCache(generations, clock=lambda: now[0])
    for seq in range(100):
        assert cache.publish(("a", 0, 1, seq, 100, b"jpeg"))
    assert len(cache.frames) == 1 and cache.latest("a")[2] == 99
    assert not cache.publish(("a", 0, 1, 10, 100, b"old"))
    assert not cache.publish(("a", 0, 0, 101, 100, b"stale"))
    generations[0] = 3
    assert cache.latest("a") is None and cache.status() == {}
    cache.publish(("a", 0, 3, 1, 100, b"current"))
    now[0] = 104
    assert cache.latest("a") is None


def test_preview_primary_path_has_no_disk_or_source_and_full_queue_keeps_latest(tmp_path):
    source, live = queue.Queue(), queue.Queue(1)
    for seq in range(1, 4):
        source.put(CameraFrame("a", 1, seq, time.perf_counter(), time.time(),
                               np.zeros((8, 8, 3), np.uint8)))
    source.put(CameraIngestSignal("a", 1, "eof"))
    with patch("cv2.VideoCapture", side_effect=AssertionError("second source")), patch(
            "fight.pipeline_mp.camera_preview.write_preview_atomic", side_effect=AssertionError("disk")):
        run_preview_consumer_loop(
            {
                "output_dir": str(tmp_path),
                "runtime": {"preview_live_max_fps": 0},
            },
            {"camera_id": "a"},
            source,
            queue.Queue(),
            threading.Event(),
            1,
            slot_id=0,
            live_channel=live,
        )
    packet = live.get_nowait()
    assert packet[3] == 3 and packet[-1].startswith(b"\xff\xd8")
    assert not list(tmp_path.iterdir())


def test_live_preview_default_throttles_jpeg_encoding(tmp_path):
    source, live = queue.Queue(), queue.Queue(4)
    captured = time.perf_counter()
    wall = time.time()
    for seq in range(1, 4):
        source.put(
            CameraFrame(
                "a",
                1,
                seq,
                captured,
                wall,
                np.zeros((8, 8, 3), np.uint8),
            )
        )
    source.put(CameraIngestSignal("a", 1, "eof"))

    with patch("fight.pipeline_mp.camera_preview.time.monotonic", return_value=100.0):
        run_preview_consumer_loop(
            {"output_dir": str(tmp_path)},
            {"camera_id": "a"},
            source,
            queue.Queue(),
            threading.Event(),
            1,
            slot_id=0,
            live_channel=live,
        )

    packet = live.get_nowait()
    assert packet[3] == 1
    with pytest.raises(queue.Empty):
        live.get_nowait()


def test_private_gateway_auth_and_two_viewers_share_latest_frame(tmp_path):
    gateway = PreviewGateway(queue.Queue(1), [1], tmp_path, "run", descriptor_path=tmp_path / "preview_gateway.json")
    connections = []
    try:
        descriptor = json.loads((tmp_path / "preview_gateway.json").read_text())
        assert descriptor["run_id"] == "run"
        unauthorized = http.client.HTTPConnection("127.0.0.1", descriptor["port"], timeout=2)
        connections.append(unauthorized)
        unauthorized.request("GET", "/status")
        assert unauthorized.getresponse().status == 403
        gateway.cache.publish(("a", 0, 1, 1, time.perf_counter(), b"jpeg-test"))
        for _ in range(2):
            connection = http.client.HTTPConnection("127.0.0.1", descriptor["port"], timeout=2)
            connections.append(connection)
            connection.request("GET", "/stream/a", headers={"Authorization": "Bearer " + gateway.token})
            response = connection.getresponse()
            assert response.status == 200
            assert b"jpeg-test" in response.read1(1024)
        assert len(gateway.cache.frames) == 1
    finally:
        for connection in connections:
            connection.close()
        gateway.close()


def test_preview_only_toggle_preserves_source_and_fences_analytics():
    services, manager, registry, now = fixture()
    enabled = camera()
    disabled = {**enabled, "use_fight_detection": False, "use_speed_detection": False}
    try:
        reconcile(services, manager, [disabled])
        item = manager.runtimes["camera"]
        original = item.processes.copy()
        assert set(original) == {"ingest", "preview"} and not services.processes()
        reconcile(services, manager, [enabled])
        epoch = item.fight_epoch
        guard = SpeedGenerationGuard(manager.slot_generations, manager.speed_epochs,
                                     item.slot_id, item.generation, item.speed_epoch)
        reconcile(services, manager, [disabled])
        assert item.fight_pause.is_set() and item.speed_stop.is_set()
        assert set(item.processes) == {"ingest", "preview"}
        stale = PersonInferenceResult(item.camera_id, item.generation, 1, 1,
                                      slot_id=item.slot_id, consumer_epoch=epoch)
        assert not is_current_generation(stale, FightGenerations(manager.slot_generations, manager.fight_publication_floor))
        assert not guard()
        reconcile(services, manager, [enabled])
        assert item.fight_epoch > epoch
        assert all(item.processes[key] is process for key, process in original.items())
        assert item.generation == 1
    finally:
        close(services, manager)


def test_file_analytics_churn_never_replays_or_becomes_clean(tmp_path):
    source = tmp_path / "input.mp4"
    source.touch()
    services, manager, registry, now = fixture()
    enabled = camera(source=str(source))
    try:
        reconcile(services, manager, [enabled])
        item = manager.runtimes["camera"]
        ingest = item.processes["ingest"]
        reconcile(services, manager, [{**enabled, "use_fight_detection": False, "use_speed_detection": False}])
        reconcile(services, manager, [enabled])
        assert item.processes["ingest"] is ingest and item.generation == 1
        assert item.fight_failed and item.speed_failed
        item.file_eof_event.set()
        ingest.alive, ingest.exitcode = False, 0
        manager.poll()
        assert item.file_done and (item.fight_failed or item.speed_failed)
    finally:
        close(services, manager)
