"""The legacy disk fan-out must reauthorize, including while frames are idle."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import include, path, reverse

from adminx.models import Location, SecurityUnit, SecurityUnitCoverage, UserSecurityAssignment
from streams.models import Camera


def _test_error(request):
    raise RuntimeError("PRIVATE_EXCEPTION_MARKER")


urlpatterns = [
    path("accounts/login/test-error/", _test_error),
    path("", include("backend_frontend_project.urls")),
]


@override_settings(DEBUG=False, ROOT_URLCONF=__name__)
class ProductionErrorTests(SimpleTestCase):
    def test_errors_use_generic_templates_without_diagnostics(self):
        client = Client(raise_request_exception=False)
        for url, status, template in (("/accounts/login/test-error/", 500, "500.html"), ("/missing/", 404, "404.html")):
            response = client.get(url)
            self.assertEqual(response.status_code, status)
            self.assertTemplateUsed(response, template)
            for diagnostic in ("PRIVATE_EXCEPTION_MARKER", "Traceback", "URL patterns", "DJANGO_SETTINGS_MODULE"):
                self.assertNotContains(response, diagnostic, status_code=status)


class LegacyStreamSecurityTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        override = override_settings(PIPELINE_OUTPUT_BASE=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("stream-security")
        self.user.profile.status = "approved"
        self.user.profile.save()
        self.location = Location.objects.create(name="Gate", code="stream-security")
        self.unit = SecurityUnit.objects.create(name="Security", code="stream-security", location=self.location)
        self.coverage = SecurityUnitCoverage.objects.create(security_unit=self.unit, location=self.location)
        self.assignment = UserSecurityAssignment.objects.create(user=self.user, security_unit=self.unit)
        self.camera = Camera.objects.create(name="Gate", camera_id="stream-security", source="rtsp://127.0.0.1/live",
                                            location=self.location, use_fight_detection=True)
        run_dir = self.root / "active-run"
        self.preview = run_dir / "previews" / "stream-security.jpg"
        self.preview.parent.mkdir(parents=True)
        self.preview.write_bytes(b"test-jpeg-frame")
        self.active = SimpleNamespace(run_id="active-run", run_dir=run_dir, runtime_state="RUNNING", process=None)
        self.client.force_login(self.user)
        self.url = reverse("dashboard:stream", args=[self.camera.camera_id])

    def assert_revocation(self, revoke):
        # Zero interval makes the production periodic check deterministic in tests.
        with patch("guvenlik.views.get_active_run", return_value=self.active), patch(
            "guvenlik.views.PREVIEW_RUNTIME_CHECK_INTERVAL_SEC", 0
        ), patch("cv2.VideoCapture") as capture:
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            frames = iter(response.streaming_content)
            self.assertIn(b"test-jpeg-frame", next(frames))
            revoke()
            # No new JPEG is needed: idle streams also terminate on revocation.
            with self.assertRaises(StopIteration):
                next(frames)
            response.close()
            self.assertIn(self.client.get(self.url).status_code, (302, 403, 404))
            capture.assert_not_called()

    def test_assignment_revocation(self):
        self.assert_revocation(lambda: UserSecurityAssignment.objects.filter(pk=self.assignment.pk).update(active=False))

    def test_unit_revocation(self):
        self.assert_revocation(lambda: SecurityUnit.objects.filter(pk=self.unit.pk).update(active=False))

    def test_coverage_revocation(self):
        self.assert_revocation(lambda: SecurityUnitCoverage.objects.filter(pk=self.coverage.pk).update(active=False))

    def test_location_revocation(self):
        self.assert_revocation(lambda: Location.objects.filter(pk=self.location.pk).update(active=False))

    def test_user_deactivation(self):
        self.assert_revocation(lambda: User.objects.filter(pk=self.user.pk).update(is_active=False))

    def test_approval_revocation(self):
        self.assert_revocation(lambda: type(self.user.profile).objects.filter(user=self.user).update(status="pending"))

    def test_camera_deactivation(self):
        self.assert_revocation(lambda: Camera.objects.filter(pk=self.camera.pk).update(is_active=False))

    def test_camera_no_longer_live(self):
        self.assert_revocation(lambda: Camera.objects.filter(pk=self.camera.pk).update(source_kind="OFFLINE"))

    def test_denied_before_first_frame_if_revoked_after_response_creation(self):
        with patch("guvenlik.views.get_active_run", return_value=self.active):
            response = self.client.get(self.url)
            self.assignment.active = False
            self.assignment.save()
            with self.assertRaises(StopIteration):
                next(iter(response.streaming_content))
            response.close()
