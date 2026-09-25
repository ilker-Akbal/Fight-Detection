"""Legacy browser status must not serialize runtime configuration."""
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from adminx.models import Location, SecurityUnit, SecurityUnitCoverage, UserSecurityAssignment
from streams.models import Camera
from speed_detection.models import SpeedCameraConfig


class PublicSpeedStatusTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = override_settings(MEDIA_ROOT=self.root, PIPELINE_OUTPUT_BASE=self.root / "runs")
        settings.enable()
        self.addCleanup(settings.disable)
        self.viewer = User.objects.create_user("speed-viewer")
        self.viewer.profile.status = "approved"
        self.viewer.profile.save()
        location = Location.objects.create(name="Gate", code="speed-security")
        unit = SecurityUnit.objects.create(name="Security", code="speed-security", location=location)
        SecurityUnitCoverage.objects.create(security_unit=unit, location=location)
        UserSecurityAssignment.objects.create(user=self.viewer, security_unit=unit)
        self.camera = Camera.objects.create(name="Gate", camera_id="speed-security", location=location,
            source="rtsp://SOURCE_USER_MARKER:SOURCE_PASSWORD_MARKER@127.0.0.1/live", use_speed_detection=True)
        SpeedCameraConfig.objects.update_or_create(camera=self.camera, defaults={"enabled": True,
            "calibration_path": str(self.root / "CALIBRATION_PATH_MARKER.json")})
        self.client.force_login(self.viewer)

    def test_real_status_projection_for_viewer_and_admin_in_both_debug_modes(self):
        admin = User.objects.create_superuser("speed-admin", "speed-admin@example.test", "Test-only!password-872")
        for user in (self.viewer, admin):
            self.client.force_login(user)
            for debug in (False, True):
                with self.subTest(role=user.username, debug=debug), override_settings(DEBUG=debug), patch(
                    "speed_detection.views.speed_runtime.get", return_value=None
                ):
                    response = self.client.get(reverse("speed_detection:status"))
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertEqual(data["camera_count"], 1)
                    self.assertEqual(data["cameras"][0]["camera_id"], self.camera.camera_id)
                    self.assertIn("calibration_ready", data["cameras"][0])
                    self.assertIn("stream_url", data["cameras"][0])
                    for secret in ("SOURCE_USER_MARKER", "SOURCE_PASSWORD_MARKER", "CALIBRATION_PATH_MARKER",
                                   '"source"', '"calibration_path"', '"run_dir"'):
                        self.assertNotContains(response, secret)

    def test_nested_rows_and_events_endpoint_use_same_allowlist(self):
        secret = "PRIVATE_RUNTIME_MARKER"
        report = {"running": True, "camera_count": 1, "run_dir": secret, "last_error": secret,
                  "cameras": [{"camera_id": "speed-security", "source": secret, "calibration_path": secret,
                               "detail": secret, "future_private_field": secret, "tracks": 3}],
                  "events": [{"camera_id": "speed-security", "speed_kmh": 42, "snapshot_url": "/speed/safe/",
                              "snapshot_path": secret, "clip_path": secret, "future_private_field": secret}],
                  "recent_status": [{"camera_id": "speed-security", "fps": 25, "detail": secret, "config": {"source": secret}}]}
        with patch("speed_detection.views._pipeline_report", return_value=report):
            for name in ("status", "events"):
                response = self.client.get(reverse("speed_detection:" + name))
                self.assertNotContains(response, secret)
                self.assertEqual(response.json()["events"][0]["speed_kmh"], 42)
                self.assertEqual(response.json()["events"][0]["snapshot_url"], "/speed/safe/")
        self.assertEqual(report["cameras"][0]["source"], secret)

    def test_control_compatibility_responses_do_not_bypass_projection(self):
        admin = User.objects.create_superuser("control-admin", "control-admin@example.test", "Test-only!password-319")
        self.client.force_login(admin)
        headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        with patch("speed_detection.views.speed_runtime.get", return_value=None):
            response = self.client.post(reverse("speed_detection:start"), **headers)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["calibration_required"])
            for marker in ("SOURCE_USER_MARKER", "SOURCE_PASSWORD_MARKER", "CALIBRATION_PATH_MARKER"):
                self.assertNotContains(response, marker, status_code=400)
        run = SimpleNamespace(process=Mock(pid=123), run_dir=self.root / "PRIVATE_RUNTIME_MARKER",
                              run_name="safe-run", cameras=[])
        for return_code in (None, 1):
            run.process.poll.return_value = return_code
            with patch("speed_detection.views.speed_runtime.get", return_value=None), patch(
                "speed_detection.views.speed_runtime.set"
            ), patch("speed_detection.views._active_speed_cameras", return_value=[{"calibration_ready": True}]), patch(
                "speed_detection.views.start_speed_pipeline", return_value=run
            ), patch("speed_detection.views.time.sleep"):
                response = self.client.post(reverse("speed_detection:start"), **headers)
                self.assertEqual(response.status_code, 200 if return_code is None else 500)
                self.assertNotIn("PRIVATE_RUNTIME_MARKER", response.content.decode())
                self.assertNotIn("run_dir", response.json())
        with patch("speed_detection.views.speed_runtime.get", return_value=None), patch(
            "speed_detection.views._active_speed_cameras", return_value=[{"calibration_ready": True}]
        ), patch("speed_detection.views.start_speed_pipeline", side_effect=RuntimeError("PRIVATE_RUNTIME_MARKER")):
            response = self.client.post(reverse("speed_detection:start"), **headers)
            self.assertNotContains(response, "PRIVATE_RUNTIME_MARKER", status_code=500)

    def test_calibration_response_does_not_expose_saved_path_or_exception(self):
        admin = User.objects.create_superuser("calibration-admin", "calibration@example.test", "Test-only!password-729")
        self.client.force_login(admin)
        url = reverse("speed_detection:save_calibration", args=[self.camera.camera_id])
        with patch("speed_detection.views.write_two_line_speed_calibration", return_value=self.root / "CALIBRATION_PATH_MARKER"), patch(
            "speed_detection.views.is_speed_calibration_ready", return_value=(True, "ok")
        ):
            response = self.client.post(url, json.dumps({}), content_type="application/json")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["calibration_ready"])
            self.assertNotContains(response, "CALIBRATION_PATH_MARKER")
        with patch("speed_detection.views.write_two_line_speed_calibration", side_effect=OSError("PRIVATE_RUNTIME_MARKER")):
            response = self.client.post(url, json.dumps({}), content_type="application/json")
            self.assertNotContains(response, "PRIVATE_RUNTIME_MARKER", status_code=400)
