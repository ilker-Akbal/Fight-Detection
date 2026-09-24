import importlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from adminx.models import Location, SecurityUnit, SecurityUnitCoverage, UserSecurityAssignment
from incidents.models import Incident, IncidentIngestCursor
from incidents.services.ingest import ingest_envelope
from services.pipeline_bridge.camera_registry import CameraRegistryReconciler, desired_camera_snapshot
from services.pipeline_bridge.offline_analysis import accessible_assets, create_run, job_root, reconcile_jobs
from streams.models import Camera, OfflineAsset, OfflineRun, OfflineResult
from fight.operations import atomic_json


class Phase25Tests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        override = override_settings(OFFLINE_JOB_DIR=self.root / "jobs", INCIDENT_OUTBOX_PATH=self.root / "outbox")
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_superuser("p25", "p25@example.test", "password")
        self.viewer = User.objects.create_user("v25")
        self.viewer.profile.role, self.viewer.profile.status = "viewer", "approved"
        self.viewer.profile.save()
        self.location = Location.objects.create(name="Gate", code="p25")
        self.live = Camera.objects.create(name="Live", camera_id="live25", source="http://127.0.0.1:8090/stream.mjpg",
                                          location=self.location, use_fight_detection=False)
        self.file = self.root / "original.mp4"
        self.file.write_bytes(b"original video bytes")
        self.old = Camera.objects.create(name="Old upload", camera_id="old25", source=str(self.file), location=self.location)
        self.client.force_login(self.admin)

    def asset(self):
        return OfflineAsset.objects.create(name="Video", file_path=str(self.file), location=self.location)

    def test_live_registry_excludes_uploads_and_keeps_mjpeg_preview_only(self):
        self.assertEqual([row["camera_id"] for row in desired_camera_snapshot()], ["live25"])
        self.assertFalse(desired_camera_snapshot()[0]["use_fight_detection"])
        Camera.objects.create(name="dup", camera_id="dup", source=self.live.source)
        from fight.runtime_supervisor.camera_state import DuplicateCameraSources
        with self.assertRaises(DuplicateCameraSources):
            desired_camera_snapshot()

    def test_autostart_follows_durable_reconcile_without_ui_and_does_not_retry_failed(self):
        client = Mock()
        client.desired_cameras.return_value = {"cameras": [], "revision": 1, "analytics_paused": True}
        client.update_desired_cameras.return_value = {"revision": 2, "accepted": True}
        client.status.return_value = {"runtime_state": "STOPPED"}
        with patch("services.pipeline_bridge.fight_runner.start_pipeline") as start:
            CameraRegistryReconciler(client).tick()
            self.assertFalse(client.update_desired_cameras.call_args.args[0]["cameras"][0]["use_fight_detection"])
            start.assert_called_once()
            client.status.return_value = {"runtime_state": "FAILED"}
            CameraRegistryReconciler(client).tick()
            start.assert_called_once()

    def test_upload_creates_asset_not_camera_then_explicit_new_runs(self):
        response = self.client.post(reverse("dashboard:offline_upload"), {"name": "Historical", "location": self.location.pk,
            "video": SimpleUploadedFile("history.mp4", b"historical-video", content_type="video/mp4")})
        self.assertEqual(response.status_code, 302)
        asset = OfflineAsset.objects.get(name="Historical")
        self.assertEqual(Camera.objects.count(), 2)
        first, second = create_run(asset, "FIGHT"), create_run(asset, "FIGHT")
        self.assertNotEqual(first.runtime_camera_id, second.runtime_camera_id)
        self.assertEqual(first.state, "QUEUED")
        self.assertEqual(self.client.get(reverse("dashboard:offline_detail", args=[asset.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("dashboard:offline_list")).status_code, 200)

    def test_scope_and_playback_ranges_fail_closed(self):
        asset = self.asset()
        url = reverse("dashboard:offline_playback", args=[asset.pk])
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(url).status_code, 404)
        unit = SecurityUnit.objects.create(name="Security", code="p25", location=self.location)
        SecurityUnitCoverage.objects.create(security_unit=unit, location=self.location)
        UserSecurityAssignment.objects.create(user=self.viewer, security_unit=unit)
        response = self.client.get(url, HTTP_RANGE="bytes=0-7")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b"".join(response.streaming_content), b"original")
        self.assertEqual(self.client.post(reverse("dashboard:offline_submit", args=[asset.pk]), {"analysis_type": "FIGHT"}).status_code, 403)
        UserSecurityAssignment.objects.all().update(active=False)
        self.assertFalse(accessible_assets(self.viewer).exists())

    def test_speed_requires_calibration_before_queue(self):
        with self.assertRaises(ValueError):
            create_run(self.asset(), "SPEED")
        self.assertFalse(OfflineRun.objects.exists())

    def test_results_import_to_historical_domain_only(self):
        run = create_run(self.asset(), "FIGHT")
        envelope = {"event_id": str(uuid.uuid4()), "run_id": "runtime", "external_incident_id": "e1",
            "camera_id": run.runtime_camera_id, "incident_type": "FIGHT", "detected_at": timezone.now().isoformat(),
            "finalized_at": timezone.now().isoformat(), "video_time_sec": 12.4, "confidence": .8}
        for _ in range(2):
            result, status = ingest_envelope(envelope, source_identifier="outbox", byte_offset=0)
            self.assertIsNone(result)
            self.assertEqual(status, "IMPORTED")
        self.assertEqual(OfflineResult.objects.count(), 1)
        self.assertEqual(OfflineResult.objects.get().video_time_sec, 12.4)
        self.assertFalse(Incident.objects.exists())

    def test_backfill_preserves_files_incidents_and_live_sources(self):
        incident = Incident.objects.create(camera=self.old, run_id="legacy", external_incident_id="legacy",
                                           detected_at=timezone.now(), finalized_at=timezone.now())
        migration = importlib.import_module("streams.migrations.0009_backfill_offline_assets")
        migration.backfill(apps, None)
        migration.backfill(apps, None)
        self.assertEqual(OfflineAsset.objects.filter(legacy_camera=self.old).count(), 1)
        self.assertEqual(self.file.read_bytes(), b"original video bytes")
        self.assertTrue(Incident.objects.filter(pk=incident.pk).exists())
        self.assertEqual(Camera.objects.get(pk=self.live.pk).source_kind, "LIVE")
        from guvenlik.presentation import visible_incidents
        self.assertFalse(visible_incidents(self.admin).exists())
        self.assertFalse(OfflineRun.objects.exists())

    def test_job_completion_waits_for_dispatcher_cursor_and_failed_is_terminal(self):
        run = create_run(self.asset(), "FIGHT")
        reconcile_jobs()
        path = job_root() / (run.runtime_camera_id + ".json")
        atomic_json(path, {"state": "COMPLETED", "started_at": 1, "updated_at": 2, "outbox_offset": 10})
        reconcile_jobs()
        run.refresh_from_db()
        self.assertEqual(run.state, "PROCESSING")
        IncidentIngestCursor.objects.create(source_identifier=str((self.root / "outbox").resolve()), byte_offset=10)
        reconcile_jobs()
        run.refresh_from_db()
        self.assertEqual(run.state, "COMPLETED")
        self.assertFalse(reconcile_jobs())
        other = create_run(run.asset, "FIGHT")
        atomic_json(job_root() / (other.runtime_camera_id + ".json"), {"state": "FAILED", "started_at": 1,
            "updated_at": 2, "error": "runtime_interrupted"})
        reconcile_jobs()
        other.refresh_from_db()
        self.assertEqual(other.state, "FAILED")

    def test_status_has_no_indefinite_verification_and_file_cards(self):
        from guvenlik.presentation import system_snapshot, camera_cards
        with patch("guvenlik.presentation.get_pipeline_status", return_value={"runtime_state": "UNKNOWN"}), patch(
                "guvenlik.presentation.get_runtime_health", return_value={"available": False, "stale": True}):
            system, health = system_snapshot()
            self.assertIn("kullanılamıyor", system["label"])
            self.assertEqual([row["camera_id"] for row in camera_cards(self.admin, system, health)], ["live25"])
