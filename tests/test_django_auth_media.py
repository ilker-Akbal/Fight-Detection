from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from adminx.forms import CameraForm
from adminx.models import FacultyLocation
from streams.models import Camera
from accounts.middleware import AuthRequiredMiddleware


pytestmark = pytest.mark.django


def _approve(user, role="viewer", faculty=None):
    profile = user.profile
    profile.status = "approved"
    profile.role = role
    profile.faculty = faculty
    profile.save()
    return user


class AuthenticationAndDashboardTests(TestCase):
    def test_anonymous_redirect_login_pending_denied_and_approved_login_logout(self):
        response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:splash"), response.url)

        pending = User.objects.create_user("pending", password="pass12345")
        response = self.client.post(reverse("accounts:login"), {
            "username": "pending", "password": "pass12345",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "admin onay")

        approved = _approve(User.objects.create_user("approved", password="pass12345"))
        response = self.client.post(reverse("accounts:login"), {
            "username": approved.username, "password": "pass12345",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("dashboard:index")).status_code, 200)
        response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_role_checks_and_empty_dashboard_start_behaviour(self):
        viewer = _approve(User.objects.create_user("viewer", password="pass12345"))
        self.client.force_login(viewer)
        response = self.client.post(
            reverse("dashboard:start_detection"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 403)

        admin = _approve(User.objects.create_user("admin", password="pass12345"), "admin")
        self.client.force_login(admin)
        response = self.client.post(
            reverse("dashboard:start_detection"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["running"])

        active = mock.Mock()
        active.process.poll.return_value = None
        active.process.pid = 123
        active.run_dir = "run"
        with mock.patch("guvenlik.views.runtime.get", return_value=active):
            response = self.client.post(
                reverse("dashboard:start_detection"),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertTrue(response.json()["already_running"])
        with mock.patch("guvenlik.views.runtime.get", return_value=None):
            response = self.client.post(
                reverse("dashboard:stop_detection"),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        self.assertTrue(response.json()["already_stopped"])

    @pytest.mark.xfail(reason="existing middleware explicitly treats all /media/ paths as public")
    def test_anonymous_direct_media_access_is_not_public(self):
        request = RequestFactory().get("/media/pipeline_runs/secret.mp4")
        request.user = AnonymousUser()
        response = AuthRequiredMiddleware(lambda req: HttpResponse("secret"))(request)
        self.assertEqual(response.status_code, 302)


class CameraPermissionAndFormTests(TestCase):
    def setUp(self):
        FacultyLocation.objects.create(name="Faculty A", code="faculty-a")
        FacultyLocation.objects.create(name="Faculty B", code="faculty-b")
        self.cam_a = Camera.objects.create(
            name="A", camera_id="cam-a", source="0", faculty="faculty-a",
            is_active=True, use_fight_detection=True,
        )
        self.cam_b = Camera.objects.create(
            name="B", camera_id="cam-b", source="1", faculty="faculty-b",
            is_active=True, use_fight_detection=True,
        )
        self.viewer = _approve(
            User.objects.create_user("viewer-a", password="pass12345"),
            faculty="faculty-a",
        )

    def test_faculty_user_sees_only_own_camera_admin_sees_all(self):
        self.client.force_login(self.viewer)
        payload = self.client.get(reverse("dashboard:status")).json()
        self.assertEqual([row["camera_id"] for row in payload["cameras"]], ["cam-a"])

        admin = _approve(User.objects.create_superuser("root", "r@example.com", "pass12345"), "admin")
        self.client.force_login(admin)
        payload = self.client.get(reverse("dashboard:status")).json()
        self.assertEqual({row["camera_id"] for row in payload["cameras"]}, {"cam-a", "cam-b"})

    def test_camera_form_source_flags_and_duplicate_camera_id(self):
        form = CameraForm(data={
            "name": "Invalid", "camera_id": "new", "source_mode": "manual",
            "source": "", "faculty": "faculty-a", "is_active": True,
            "use_fight_detection": True, "use_speed_detection": False,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("source", form.errors)

        valid = CameraForm(data={
            "name": "Valid", "camera_id": "new", "source_mode": "manual",
            "source": "rtsp://example", "faculty": "faculty-a", "is_active": True,
            "use_fight_detection": False, "use_speed_detection": True,
        })
        self.assertTrue(valid.is_valid(), valid.errors)
        camera = valid.save()
        self.assertFalse(camera.use_fight_detection)
        self.assertTrue(camera.use_speed_detection)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Camera.objects.create(name="Duplicate", camera_id="new", source="2")

    def test_admin_camera_create_edit_delete_flow(self):
        admin = _approve(User.objects.create_user("admin", password="pass12345"), "admin")
        self.client.force_login(admin)
        payload = {
            "name": "Created", "camera_id": "created", "source_mode": "manual",
            "source": "0", "faculty": "faculty-a", "is_active": "on",
            "use_fight_detection": "on", "speed_limit_kmh": "50",
            "tolerance_kmh": "10", "roi_polygon_text": "[]",
        }
        response = self.client.post(reverse("adminx:camera_create"), payload)
        self.assertEqual(response.status_code, 302)
        camera = Camera.objects.get(camera_id="created")
        self.assertTrue(camera.use_fight_detection)

        payload.update({"name": "Edited", "source": "2", "use_speed_detection": "on"})
        response = self.client.post(reverse("adminx:camera_edit", args=[camera.pk]), payload)
        self.assertEqual(response.status_code, 302)
        camera.refresh_from_db()
        self.assertEqual(camera.name, "Edited")
        self.assertTrue(camera.use_speed_detection)

        self.assertEqual(
            self.client.post(reverse("adminx:camera_delete", args=[camera.pk])).status_code,
            302,
        )
        self.assertFalse(Camera.objects.filter(pk=camera.pk).exists())


class IncidentVideoEndpointTests(TestCase):
    def setUp(self):
        self.run_name = "run-safe"
        self.clip_name = "incident.mp4"
        self.data = b"0123456789"
        self.clip = (
            Path(settings.MEDIA_ROOT) / "pipeline_runs" / self.run_name /
            "incidents" / self.clip_name
        )
        self.clip.parent.mkdir(parents=True, exist_ok=True)
        self.clip.write_bytes(self.data)
        Camera.objects.create(
            name="A", camera_id="cam-a", source="0", faculty="faculty-a",
            is_active=True, use_fight_detection=True,
        )
        self.viewer = _approve(
            User.objects.create_user("viewer", password="pass12345"),
            faculty="faculty-a",
        )
        self.other = _approve(
            User.objects.create_user("other", password="pass12345"),
            faculty="faculty-b",
        )
        self.report = {"recent_incidents": [{
            "run_name": self.run_name, "camera_id": "cam-a",
            "clip_name": self.clip_name,
        }]}
        self.url = reverse("dashboard:incident_video", args=[self.run_name, self.clip_name])

    def test_authenticated_full_range_invalid_range_missing_and_unauthorized(self):
        self.client.force_login(self.viewer)
        with mock.patch("guvenlik.views._pipeline_report", return_value=self.report):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "video/mp4")
            self.assertEqual(response["Accept-Ranges"], "bytes")
            self.assertEqual(b"".join(response.streaming_content), self.data)

            response = self.client.get(self.url, HTTP_RANGE="bytes=2-5")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b"2345")
            self.assertEqual(response["Content-Range"], "bytes 2-5/10")

            response = self.client.get(self.url, HTTP_RANGE="bytes=20-")
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response["Content-Range"], "bytes */10")

            response = self.client.get(self.url, HTTP_RANGE="malformed")
            self.assertEqual(response.status_code, 200)

        self.client.force_login(self.other)
        with mock.patch("guvenlik.views._pipeline_report", return_value=self.report):
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.viewer)
        missing = reverse("dashboard:incident_video", args=[self.run_name, "missing.mp4"])
        with mock.patch("guvenlik.views._can_access_incident_clip", return_value=True):
            self.assertEqual(self.client.get(missing).status_code, 404)

    @pytest.mark.xfail(reason="existing endpoint does not reject an end byte lower than start")
    def test_reversed_range_is_rejected(self):
        self.client.force_login(self.viewer)
        with mock.patch("guvenlik.views._pipeline_report", return_value=self.report):
            self.assertEqual(self.client.get(self.url, HTTP_RANGE="bytes=8-2").status_code, 416)

    @pytest.mark.xfail(reason="existing admin path does not validate run_name containment")
    def test_admin_run_name_path_traversal_is_rejected(self):
        admin = _approve(User.objects.create_superuser("root", "r@example.com", "pass12345"), "admin")
        self.client.force_login(admin)
        escaped = Path(settings.MEDIA_ROOT) / "incidents" / "secret.mp4"
        escaped.parent.mkdir(parents=True, exist_ok=True)
        escaped.write_bytes(b"secret")
        url = reverse("dashboard:incident_video", args=["..", "secret.mp4"])
        self.assertEqual(self.client.get(url).status_code, 404)


class StreamingAndSpeedAuthorizationTests(TestCase):
    def setUp(self):
        self.viewer = _approve(
            User.objects.create_user("viewer", password="pass12345"), faculty="faculty-a"
        )
        Camera.objects.create(
            name="A", camera_id="cam-a", source="0", faculty="faculty-a",
            is_active=True, use_fight_detection=True,
        )
        Camera.objects.create(
            name="B", camera_id="cam-b", source="1", faculty="faculty-b",
            is_active=True, use_fight_detection=True,
        )

    def test_stream_authorization_mime_and_unavailable_camera(self):
        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.get(reverse("dashboard:stream", args=["cam-b"])).status_code,
            404,
        )
        capture = mock.Mock()
        capture.isOpened.return_value = True
        with mock.patch("guvenlik.views._open_capture", return_value=capture):
            response = self.client.get(reverse("dashboard:stream", args=["cam-a"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("multipart/x-mixed-replace", response["Content-Type"])
        capture.release.assert_called_once()
        with mock.patch("guvenlik.views._open_capture", return_value=None):
            self.assertEqual(
                self.client.get(reverse("dashboard:stream", args=["cam-a"])).status_code,
                404,
            )

    @pytest.mark.xfail(reason="existing speed media endpoint checks login but not faculty ownership")
    def test_speed_clip_from_another_faculty_is_denied(self):
        self.client.force_login(self.viewer)
        clip = Path(settings.MEDIA_ROOT) / "speed_runs" / "other-run" / "clips" / "clip.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"video")
        url = reverse("speed_detection:clip", args=["other-run", "clip.mp4"])
        self.assertEqual(self.client.get(url).status_code, 404)
