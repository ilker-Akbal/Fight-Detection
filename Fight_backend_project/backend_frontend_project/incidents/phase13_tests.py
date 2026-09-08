import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from fight.pipeline_mp.speed_worker import persist_speed_event, SpeedGenerationGuard
from fight.pipeline_mp.scheduling import AdmissionStopped
from fight.runtime_supervisor.camera_state import DesiredCameraStateStore
from HizTespiti.speed.src.evidence_writer import SpeedViolationEvent
from incidents.models import Incident, IncidentIngestRecord
from incidents.services.ingest import dispatcher_tick
from services.pipeline_bridge.camera_registry import desired_camera_snapshot, CameraRegistryReconciler
from services.speed_bridge.speed_runner import _set_speed_paused, start_speed_pipeline, stop_speed_pipeline, get_active_speed_run
from speed_detection.models import SpeedCameraConfig
from streams.models import Camera


class Phase13IntegrationTests(TestCase):
    def test_registry_pause_and_speed_outbox_use_existing_domains(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cameras = []
            for name, fight, speed in (("fight", True, False), ("speed", False, True), ("both", True, True), ("neither", False, False)):
                cam = Camera.objects.create(name=name, camera_id=name, source=f"rtsp://host/{name}",
                                            use_fight_detection=fight, use_speed_detection=speed)
                SpeedCameraConfig.objects.create(camera=cam, enabled=speed, calibration_path=str(root / "calibration.json"))
                cameras.append(cam)
            with override_settings(MEDIA_ROOT=root, INCIDENT_EVIDENCE_ROOTS=[root / "pipeline_runs"]):
                desired = desired_camera_snapshot()
                assert {item["camera_id"] for item in desired} == {"fight", "speed", "both"}
                both = next(item for item in desired if item["camera_id"] == "both")
                assert both["use_fight_detection"] and both["use_speed_detection"] and both["speed_config"]
                store = DesiredCameraStateStore(root / "desired.json")
                class Client:
                    desired_cameras = staticmethod(store.load)
                    def update_desired_cameras(self, payload):
                        update = store.update(payload)
                        return {**update.state, "accepted": update.changed}
                client = Client()
                reconciler = CameraRegistryReconciler(client)
                reconciler.tick()
                with patch("services.pipeline_bridge.camera_registry.supervisor_client", return_value=client):
                    _set_speed_paused(True)
                    reconciler.tick()
                    assert store.load()["speed_paused"] and not any(item["use_speed_detection"] for item in store.load()["cameras"])
                    _set_speed_paused(False)
                    reconciler.tick()
                    assert next(item for item in store.load()["cameras"] if item["camera_id"] == "both")["use_speed_detection"]
                    active = SimpleNamespace(runtime_pid=321, run_name="run", run_dir=root,
                        config_path=root / "config.json", stdout_path=root / "stdout.log",
                        stderr_path=root / "stderr.log", started_at=time.time())
                    with patch("services.pipeline_bridge.fight_runner._control_mode", return_value="supervisor"), patch(
                        "services.pipeline_bridge.fight_runner.start_pipeline", return_value=active) as start, patch(
                        "subprocess.Popen", side_effect=AssertionError("Django must not spawn Speed")):
                        handle = start_speed_pipeline()
                        assert handle.process.pid == 321 and start.call_count == 1
                        stop_speed_pipeline(handle)
                        assert store.load()["speed_paused"]
                    _set_speed_paused(False)
                    reconciler.tick()
                    with patch("services.pipeline_bridge.fight_runner.get_pipeline_status", return_value={
                        "runtime_state": "RUNNING", "runtime_pid": 321}), patch(
                        "services.pipeline_bridge.fight_runner.get_active_run", return_value=active):
                        assert get_active_speed_run().process.pid == 321
                output = root / "pipeline_runs" / "run"
                output.mkdir(parents=True)
                evidence = output / "speed.mp4"
                evidence.write_bytes(b"durable evidence fixture")
                outbox = root / "spool" / "outbox.jsonl"
                event = SpeedViolationEvent("speed", 25, 1, 4, "car", 75, 50, 10, 60, [0, 0, 5, 5], None, str(evidence), time.time())
                config = {"run_id": "run", "output_dir": str(output), "runtime": {"incident_outbox_path": str(outbox)}}
                guard = SpeedGenerationGuard([1], [2], 0, 1, 2)
                persist_speed_event(config, {"camera_id": "speed"}, event, 1, 2, guard)
                dispatcher_tick(outbox)
                incident = Incident.objects.get()
                assert incident.incident_type == Incident.TYPE_SPEED and incident.camera_id == cameras[1].pk
                assert incident.evidence_valid
                record = IncidentIngestRecord.objects.get(incident=incident)
                assert record.raw_envelope["speed"]["speed_kmh"] == 75
                assert record.raw_envelope["speed"]["speed_limit_kmh"] == 50
                guard.epochs[0] = 3
                before = outbox.read_bytes()
                with self.assertRaises(AdmissionStopped):
                    persist_speed_event(config, {"camera_id": "speed"}, event, 1, 2, guard)
                assert outbox.read_bytes() == before
