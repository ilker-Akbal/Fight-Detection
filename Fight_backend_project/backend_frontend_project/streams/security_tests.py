"""Generic media is not an alternate authorization path or artifact browser."""
import importlib
import tempfile
import uuid
from pathlib import Path

from django.contrib.auth.models import AnonymousUser, User
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import clear_url_caches
from django.utils import timezone

from adminx.models import Location, SecurityUnit, SecurityUnitCoverage, UserSecurityAssignment
from incidents.models import Incident
from streams.models import Camera, OfflineAsset, OfflineRun, OfflineResult
from streams.protected_media import scoped_media


class ScopedMediaSecurityTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        override = override_settings(MEDIA_ROOT=self.media, INCIDENT_EVIDENCE_ROOTS=[self.media / "evidence"])
        override.enable()
        self.addCleanup(override.disable)
        self.viewer = User.objects.create_user("media-viewer")
        self.viewer.profile.status = "approved"
        self.viewer.profile.save()
        self.outsider = User.objects.create_user("media-outsider")
        self.outsider.profile.status = "approved"
        self.outsider.profile.save()
        self.location = Location.objects.create(name="Gate", code="media-security")
        unit = SecurityUnit.objects.create(name="Security", code="media-security", location=self.location)
        SecurityUnitCoverage.objects.create(security_unit=unit, location=self.location)
        self.assignment = UserSecurityAssignment.objects.create(user=self.viewer, security_unit=unit)
        self.live = Camera.objects.create(name="Live", camera_id="media-live", source="rtsp://127.0.0.1/live", location=self.location)
        self.historical = Camera.objects.create(name="Historical", camera_id="media-old", source=str(self.media / "original.mp4"), location=self.location)
        self.asset = OfflineAsset.objects.create(name="Asset", file_path=str(self.file("original.mp4")), location=self.location)
        run = OfflineRun.objects.create(asset=self.asset, analysis_type="FIGHT")
        self.file("evidence/result.mp4")
        OfflineResult.objects.create(run=run, event_id=uuid.uuid4(), analysis_type="FIGHT", evidence_path="evidence/result.mp4")
        for camera, name in ((self.live, "live"), (self.historical, "historical")):
            self.file(f"evidence/{name}.mp4")
            Incident.objects.create(camera=camera, run_id="security", external_incident_id=name,
                detected_at=timezone.now(), finalized_at=timezone.now(), evidence_valid=True,
                evidence_path=f"evidence/{name}.mp4")

    def file(self, name):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MEDIA_TEST_MARKER")
        return path.resolve()

    def request(self, name, user):
        request = RequestFactory().get("/dashboard/media/" + name)
        request.user = user
        return scoped_media(request, name, document_root=self.media)

    def test_recognized_objects_allow_scope_and_deny_outsiders_in_both_modes(self):
        for debug in (True, False):
            with override_settings(DEBUG=debug):
                for name in ("original.mp4", "evidence/result.mp4", "evidence/live.mp4", "evidence/historical.mp4"):
                    with self.subTest(debug=debug, media=name):
                        response = self.request(name, self.viewer)
                        self.assertEqual(b"".join(response.streaming_content), b"MEDIA_TEST_MARKER")
                        response.close()
                        for user in (self.outsider, AnonymousUser()):
                            with self.assertRaises(Http404):
                                self.request(name, user)

    def test_unknown_operational_and_unregistered_media_deny_even_admin(self):
        admin = User.objects.create_superuser("media-admin", "media-admin@example.test", "Test-only!password-923")
        for debug in (True, False):
            with override_settings(DEBUG=debug):
                for name in ("unknown.mp4", "run_config.json", "stderr.log", "runtime_health.json",
                             "runtime_spool/status.json", "previews/camera.jpg"):
                    self.file(name)
                    for user in (admin, self.viewer, self.outsider):
                        with self.subTest(debug=debug, media=name, user=user.username), self.assertRaises(Http404):
                            self.request(name, user)

    def test_containment_and_invalid_incident_evidence(self):
        (self.root / "outside.mp4").write_bytes(b"outside")
        with self.assertRaises(Http404):
            self.request("../outside.mp4", self.viewer)
        with self.assertRaises(Http404):
            self.request(str(self.root / "outside.mp4"), self.viewer)
        Incident.objects.filter(external_incident_id="live").update(evidence_valid=False)
        with self.assertRaises(Http404):
            self.request("evidence/live.mp4", self.viewer)

    def test_revoked_scope_denies_registered_media(self):
        self.assignment.active = False
        self.assignment.save()
        for name in ("original.mp4", "evidence/result.mp4", "evidence/live.mp4", "evidence/historical.mp4"):
            with self.assertRaises(Http404):
                self.request(name, self.viewer)

    def test_development_route_uses_guard_and_production_route_is_absent(self):
        from guvenlik import urls as dashboard_urls
        from backend_frontend_project import urls as root_urls
        self.client.force_login(self.viewer)
        try:
            for debug in (True, False):
                with override_settings(DEBUG=debug):
                    importlib.reload(dashboard_urls)
                    importlib.reload(root_urls)
                    clear_url_caches()
                    response = self.client.get("/dashboard/media/evidence/live.mp4")
                    self.assertEqual(response.status_code, 200 if debug else 404)
                    response.close()
                    self.assertEqual(self.client.get("/dashboard/media/run_config.json").status_code, 404)
        finally:
            importlib.reload(dashboard_urls)
            importlib.reload(root_urls)
            clear_url_caches()
