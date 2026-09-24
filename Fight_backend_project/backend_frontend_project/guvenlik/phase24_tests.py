"""Focused Phase 24 control, access and presentation regressions (no GPU)."""
import tempfile
import json
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.views.defaults import server_error
from django.test import RequestFactory

from adminx.forms import CameraForm, UserEditForm
from adminx.models import Location, SecurityUnit, SecurityUnitCoverage, UserSecurityAssignment
from fight.runtime_supervisor.camera_state import DesiredCameraStateStore
from services.access_scope import get_user_accessible_cameras
from services.pipeline_bridge.camera_registry import CameraRegistryReconciler
from streams.models import Camera


class Phase24Tests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = override_settings(OFFLINE_JOB_DIR=Path(temporary.name) / "offline")
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_superuser("admin24", "admin24@example.test", "A-long-test-password-24")
        self.admin.profile.status, self.admin.profile.role = "approved", "admin"
        self.admin.profile.save()
        self.viewer = User.objects.create_user("viewer24", password="A-long-test-password-24")
        self.viewer.profile.role, self.viewer.profile.status = "viewer", "approved"
        self.viewer.profile.save()
        self.root = Location.objects.create(name="Campus", code="campus24")
        self.child = Location.objects.create(name="Gate", code="gate24", parent=self.root)
        self.unit = SecurityUnit.objects.create(name="Campus security", code="campus-security24", location=self.root)
        SecurityUnitCoverage.objects.create(security_unit=self.unit, location=self.root, include_descendants=True)
        self.camera = Camera.objects.create(name="Gate camera", camera_id="cam24", source="rtsp://private/stream",
                                            location=self.child, faculty="orphan-old-location")
        self.control = patch("guvenlik.presentation.get_pipeline_status", return_value={"runtime_state": "STOPPED"}).start()
        self.health = patch("guvenlik.presentation.get_runtime_health", return_value={"stale": True}).start()
        self.preview = patch("services.pipeline_bridge.live_preview.preview_status", return_value={}).start()
        self.addCleanup(patch.stopall)
        self.client.force_login(self.admin)

    def edit_data(self, units):
        return {"username": self.viewer.username, "email": "", "faculty": "", "role": "viewer",
                "status": "approved", "security_units": units}

    def test_assignment_edit_reuses_rows_and_descendant_access(self):
        self.assertFalse(get_user_accessible_cameras(self.viewer).exists())
        for selected in ([self.unit.pk], [], [self.unit.pk], [self.unit.pk]):
            form = UserEditForm(self.edit_data(selected), instance=self.viewer.profile, user_instance=self.viewer)
            self.assertTrue(form.is_valid(), form.errors)
            form.save(self.viewer)
            self.assertEqual(get_user_accessible_cameras(self.viewer).exists(), bool(selected))
        self.assertEqual(UserSecurityAssignment.objects.filter(user=self.viewer).count(), 1)
        form = UserEditForm(instance=self.viewer.profile, user_instance=self.viewer)
        self.assertEqual(form.fields["security_units"].initial, [self.unit.pk])

    def test_admin_create_assigns_unit_and_viewer_cannot_change_access(self):
        data = {**self.edit_data([self.unit.pk]), "username": "newviewer24",
                "password1": "A-long-test-password-25", "password2": "A-long-test-password-25"}
        response = self.client.post(reverse("adminx:user_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        user = User.objects.get(username="newviewer24")
        self.assertTrue(user.check_password(data["password1"]))
        self.assertEqual(list(get_user_accessible_cameras(user)), [self.camera])
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(reverse("adminx:user_create"), data).status_code, 403)
        self.assertEqual(self.client.post(reverse("dashboard:analytics_control", args=["stop"])).status_code, 403)

    def test_unassigned_viewer_clear_message_and_no_stream_access(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, "güvenlik erişimi atanmamış")
        self.assertNotContains(response, "Gate camera")
        self.assertEqual(self.client.get(reverse("dashboard:live_preview", args=["cam24"])).status_code, 404)

    def test_legacy_camera_edit_uses_canonical_location(self):
        form = CameraForm({"name": "Renamed", "camera_id": "cam24", "source_mode": "manual",
            "source": self.camera.source, "location": self.child.pk, "faculty": self.camera.faculty,
            "is_active": True}, instance=self.camera)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.faculty, self.child.code)

    def test_login_redirect_and_explicit_next(self):
        self.client.logout()
        url = reverse("accounts:login")
        credentials = {"username": self.admin.username, "password": "A-long-test-password-24"}
        self.assertRedirects(self.client.post(url, credentials), reverse("adminx:dashboard"), fetch_redirect_response=False)
        self.client.logout()
        self.assertRedirects(self.client.post(url, {**credentials, "next": "/dashboard/history/"}),
                             "/dashboard/history/", fetch_redirect_response=False)

    @override_settings(DEBUG=False)
    def test_public_reset_and_safe_error_pages(self):
        self.client.logout()
        response = self.client.get(reverse("accounts:password_reset_request"))
        for label in ("Hesabım", "Çıkış Yap", "Canlı İzleme", "global-status", "workspace.js"):
            self.assertNotContains(response, label)
        response = self.client.get("/not-a-real-page-phase24/")
        self.assertContains(response, "Sayfa bulunamadı", status_code=404)
        self.assertNotContains(response, "URL patterns", status_code=404)
        response = server_error(RequestFactory().get("/"))
        self.assertContains(response, "İşlem tamamlanamadı", status_code=500)

    def test_fresh_stale_offline_and_analytics_are_independent(self):
        self.control.return_value = {"runtime_state": "RUNNING", "analytics_paused": True,
                                     "desired_camera_revision": 4}
        self.health.return_value = {"stale": False, "runtime_health": "HEALTHY", "desired_camera_revision": 4,
            "workers": {name: {"service_state": "disabled"} for name in ("person", "pose", "stage3", "vehicle")},
            "cameras": {"cam24": {"generation": 1, "health": "ONLINE", "fight": {"enabled": False},
                                    "speed": {"enabled": False}, "last_preview_publish_age_sec": .2}}}
        response = self.client.get(reverse("dashboard:overview"))
        self.assertEqual(response.json()["cameras"][0]["label"], "Canlı")
        self.assertIn("Kavga: Kapalı", response.json()["cameras"][0]["analysis"])
        self.assertTrue(response.json()["system"]["analytics_confirmed"])
        self.health.return_value["desired_camera_revision"] = 3
        self.assertFalse(self.client.get(reverse("dashboard:overview")).json()["system"]["analytics_confirmed"])
        row = self.health.return_value["cameras"]["cam24"]
        row["last_preview_publish_age_sec"] = 12
        self.assertEqual(self.client.get(reverse("dashboard:overview")).json()["cameras"][0]["label"], "Görüntü alınamıyor")
        row.update(last_preview_publish_age_sec=None, source_state="offline")
        self.assertEqual(self.client.get(reverse("dashboard:overview")).json()["cameras"][0]["tone"], "danger")

    def test_stop_persists_preview_only_and_reconciler_does_not_undo_it(self):
        from services.pipeline_bridge.analytics_control import set_analytics
        with tempfile.TemporaryDirectory() as temporary:
            store = DesiredCameraStateStore(Path(temporary) / "desired.json")
            client = Mock()
            client.desired_cameras.side_effect = store.load
            client.update_desired_cameras.side_effect = lambda data: store.update(data).state
            with patch("services.pipeline_bridge.analytics_control.supervisor_client", return_value=client), patch(
                    "services.pipeline_bridge.analytics_control.get_pipeline_status", return_value={"runtime_state": "RUNNING"}), patch(
                    "services.pipeline_bridge.analytics_control.start_pipeline") as start:
                set_analytics(True)
                CameraRegistryReconciler(client).tick()
                desired = store.load()
                self.assertTrue(desired["analytics_paused"])
                self.assertEqual(len(desired["cameras"]), 1)
                self.assertFalse(desired["cameras"][0]["use_fight_detection"])
                self.assertTrue(desired["cameras"][0]["enabled"])
                start.assert_not_called()
                set_analytics(False)
                self.assertTrue(store.load()["cameras"][0]["use_fight_detection"])

    def test_stopped_starts_either_mode_through_authenticated_supervisor_and_hard_stop(self):
        from fight.runtime_supervisor.core import RuntimeSupervisor, SupervisorConfig
        from fight.runtime_supervisor.http_api import create_http_server
        from fight.runtime_supervisor.client import RuntimeSupervisorClient
        from tests.test_runtime_supervisor import Factory
        from services.pipeline_bridge.analytics_control import set_analytics
        for paused in (True, False):
            with self.subTest(paused=paused), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                factory = Factory()
                supervisor = RuntimeSupervisor(SupervisorConfig(repo_root=root, state_dir=root / "state",
                    allowed_config_dirs=(root,), stop_grace_sec=.01, kill_grace_sec=.01),
                    popen_factory=factory, platform_name="nt", start_monitor=False)
                server = create_http_server(supervisor, host="127.0.0.1", port=0, token="test-only-token")
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                thread.start()
                client = RuntimeSupervisorClient(f"http://127.0.0.1:{server.server_port}", "test-only-token")
                try:
                    with override_settings(PIPELINE_OUTPUT_BASE=root / "runs", PIPELINE_CONTROL_MODE="supervisor"), patch(
                            "services.pipeline_bridge.analytics_control.supervisor_client", return_value=client), patch(
                            "services.pipeline_bridge.analytics_control.get_pipeline_status", side_effect=client.status), patch(
                            "services.pipeline_bridge.fight_runner._supervisor_client", return_value=client):
                        self.assertEqual(client.status()["runtime_state"], "STOPPED")
                        set_analytics(paused)
                        state = client.status()
                        self.assertEqual(state["runtime_state"], "RUNNING")
                        launch = json.loads(Path(supervisor._state["launch_config_path"]).read_text())
                        self.assertTrue(launch["runtime"]["preview_live_enabled"])
                        self.assertEqual(launch["cameras"][0]["use_fight_detection"], not paused)
                        self.assertTrue(launch["cameras"][0]["enabled"])
                        self.assertEqual(client.desired_cameras()["analytics_paused"], paused)
                        set_analytics(not paused)
                        self.assertEqual(client.status()["runtime_pid"], state["runtime_pid"])
                        self.assertEqual(len(factory.calls), 1)
                        self.assertEqual(client.stop()["runtime_state"], "STOPPED")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(1)
                    supervisor.close(stop_runtime=True)

    def test_duplicate_sources_reject_before_supervisor_write_with_safe_diagnostics(self):
        Camera.objects.create(camera_id="duplicate24", name="Duplicate", source=self.camera.source)
        with patch("services.pipeline_bridge.analytics_control.supervisor_client") as supervisor, self.assertLogs(
                "services.pipeline_bridge.analytics_control", level="WARNING") as logs:
            response = self.client.post(reverse("dashboard:analytics_control", args=["stop"]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "duplicate_camera_sources")
        self.assertEqual(response.json()["stage"], "camera_registry")
        self.assertEqual(response.json()["camera_groups"], [["cam24", "duplicate24"]])
        supervisor.assert_not_called()
        self.assertNotIn("rtsp://", str(response.json()) + str(logs.output))
        self.assertNotIn("private", str(response.json()) + str(logs.output))

    def test_supervisor_rejection_is_explicit_and_not_success(self):
        from fight.runtime_supervisor.client import SupervisorRequestError
        supervisor = Mock()
        supervisor.desired_cameras.side_effect = SupervisorRequestError("unauthorized", http_status=401)
        with patch("services.pipeline_bridge.analytics_control.supervisor_client", return_value=supervisor), patch(
                "services.pipeline_bridge.analytics_control.get_pipeline_status", return_value={"runtime_state": "STOPPED"}):
            response = self.client.post(reverse("dashboard:analytics_control", args=["start"]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "supervisor_unauthorized")
        self.assertEqual(response.json()["supervisor_http_status"], 401)
        self.assertEqual(response.json()["stage"], "desired_read")
        supervisor.update_desired_cameras.assert_not_called()

    def test_mode_labels_and_stopped_service_rows(self):
        response = self.client.get(reverse("dashboard:overview"))
        self.assertEqual(response.json()["system"]["label"], "Sistem Teknik Olarak Durduruldu")
        self.assertTrue(all(row["health"] == "Durduruldu" for row in response.json()["workers"]))
        self.control.return_value = {"runtime_state": "RUNNING", "analytics_paused": True, "desired_camera_revision": 2}
        self.health.return_value = {"runtime_health": "HEALTHY", "stale": False, "desired_camera_revision": 2,
            "cameras": {"cam24": {"fight": {"enabled": False}, "speed": {"enabled": False}}},
            "workers": {name: {"service_state": "disabled"} for name in ("person", "pose", "stage3", "vehicle")}}
        self.assertEqual(self.client.get(reverse("dashboard:overview")).json()["system"]["label"],
                         "Kameralar Aktif · Analizler Durduruldu")
        self.control.return_value["analytics_paused"] = False
        self.assertEqual(self.client.get(reverse("dashboard:overview")).json()["system"]["label"],
                         "Kameralar Aktif · Analizler Aktif")

    def test_stream_is_authenticated_relay_not_a_source(self):
        with patch("services.pipeline_bridge.live_preview.gateway_context", return_value={"run_id": "test"}), patch(
                "services.pipeline_bridge.live_preview.stream", return_value=iter([b"jpeg"])) as stream:
            response = self.client.get(reverse("dashboard:live_preview", args=["cam24"]))
            self.assertEqual(response["Content-Type"], "multipart/x-mixed-replace; boundary=frame")
            self.assertEqual(b"".join(response.streaming_content), b"jpeg")
            self.assertTrue(stream.call_args.args[1]())

    def test_preview_only_multiplex_stream_keeps_camera_authorization(self):
        with patch("services.pipeline_bridge.live_preview.gateway_context", return_value={"run_id": "test"}), patch(
                "services.pipeline_bridge.live_preview.stream", return_value=iter([b"3 cam24\njpg"])) as stream, patch(
                "cv2.VideoCapture", side_effect=AssertionError("second source opener")):
            response = self.client.get(reverse("dashboard:live_previews"), {"camera": ["cam24"]})
            self.assertEqual(response["Content-Type"], "application/x-camera-frames")
            self.assertEqual(b"".join(response.streaming_content), b"3 cam24\njpg")
            self.assertTrue(stream.call_args.kwargs["multiplex"])
            self.assertEqual(self.client.get(reverse("dashboard:live_previews"), {"camera": ["cam24", "outside"]}).status_code, 404)
            self.client.force_login(self.viewer)
            self.assertEqual(self.client.get(reverse("dashboard:live_previews"), {"camera": ["cam24"]}).status_code, 404)

    def test_calibration_snapshots_use_only_runtime_preview(self):
        import cv2
        import numpy as np
        from speed_detection.models import SpeedCameraConfig
        SpeedCameraConfig.objects.create(camera=self.camera)
        ok, encoded = cv2.imencode(".jpg", np.zeros((8, 12, 3), dtype=np.uint8))
        self.assertTrue(ok)
        with patch("services.pipeline_bridge.live_preview.snapshot", return_value=encoded.tobytes()), patch(
                "cv2.VideoCapture", side_effect=AssertionError("second source opener")):
            for url in (reverse("adminx:camera_preview_frame", args=[self.camera.pk]),
                        reverse("speed_detection:calibration_frame", args=["cam24"])):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(b"".join(response.streaming_content).startswith(b"\xff\xd8"))
