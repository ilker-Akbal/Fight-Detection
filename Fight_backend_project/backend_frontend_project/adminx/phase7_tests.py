from io import StringIO
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from adminx.forms import CameraForm
from adminx.models import (
    Location,
    SecurityUnit,
    SecurityUnitCoverage,
    UserSecurityAssignment,
)
from services.access_scope import (
    get_user_accessible_cameras,
    get_user_location_scope,
    get_user_security_units,
    user_can_access_camera,
    user_can_manage_camera,
)
from speed_detection.models import SpeedCameraConfig
from streams.models import Camera


class LocationTreeTests(TestCase):
    def setUp(self):
        self.campus = Location.objects.create(name="Campus", code="campus", location_type="campus")
        self.faculty = Location.objects.create(
            name="Faculty", code="faculty", location_type="faculty", parent=self.campus
        )
        self.building = Location.objects.create(
            name="Building", code="building", location_type="building", parent=self.faculty
        )
        self.floor = Location.objects.create(
            name="Floor", code="floor", location_type="floor", parent=self.building
        )

    def test_01_self_parent_is_rejected(self):
        self.campus.parent = self.campus
        with self.assertRaises(ValidationError):
            self.campus.save()

    def test_02_indirect_cycle_is_rejected(self):
        self.campus.parent = self.floor
        with self.assertRaises(ValidationError):
            self.campus.save()

    def test_03_ancestors_are_returned(self):
        self.assertSetEqual(
            set(self.floor.get_ancestors().values_list("code", flat=True)),
            {"campus", "faculty", "building"},
        )

    def test_04_ancestors_can_include_self(self):
        codes = set(self.floor.get_ancestors(include_self=True).values_list("code", flat=True))
        self.assertEqual(codes, {"campus", "faculty", "building", "floor"})

    def test_05_descendants_are_returned(self):
        codes = set(self.campus.get_descendants().values_list("code", flat=True))
        self.assertEqual(codes, {"faculty", "building", "floor"})

    def test_06_descendant_relationship_is_directional(self):
        self.assertTrue(self.floor.is_descendant_of(self.campus))
        self.assertFalse(self.campus.is_descendant_of(self.floor))
        self.assertFalse(self.campus.is_descendant_of(self.campus))

    def test_07_inactive_branch_is_not_traversed_in_active_mode(self):
        self.faculty.active = False
        self.faculty.save()
        self.assertFalse(self.campus.get_descendants(active_only=True).filter(pk=self.floor.pk).exists())

    def test_08_inactive_ancestor_makes_child_effectively_inactive(self):
        self.faculty.active = False
        self.faculty.save()
        self.assertFalse(self.floor.is_effectively_active())

    def test_09_parent_with_children_is_protected_from_delete(self):
        with self.assertRaises(ProtectedError):
            self.campus.delete()

    def test_10_custom_location_type_is_allowed(self):
        custom = Location.objects.create(name="Tunnel", code="tunnel", location_type="secure_tunnel")
        self.assertEqual(custom.location_type, "secure_tunnel")


class AccessScopeTests(TestCase):
    def setUp(self):
        self.campus = Location.objects.create(name="Campus", code="campus", location_type="campus")
        self.faculty_a = Location.objects.create(
            name="Faculty A", code="faculty-a", location_type="faculty", parent=self.campus
        )
        self.building_a = Location.objects.create(
            name="Building A", code="building-a", location_type="building", parent=self.faculty_a
        )
        self.room_a = Location.objects.create(
            name="Room A", code="room-a", location_type="room", parent=self.building_a
        )
        self.faculty_b = Location.objects.create(
            name="Faculty B", code="faculty-b", location_type="faculty", parent=self.campus
        )
        self.building_b = Location.objects.create(
            name="Building B", code="building-b", location_type="building", parent=self.faculty_b
        )
        self.inactive_branch = Location.objects.create(
            name="Inactive", code="inactive", location_type="building", parent=self.campus, active=False
        )
        self.inactive_child = Location.objects.create(
            name="Inactive Child", code="inactive-child", location_type="floor", parent=self.inactive_branch
        )

        self.cam_a = self.camera("cam-a", self.faculty_a)
        self.cam_a_child = self.camera("cam-a-child", self.room_a)
        self.cam_b = self.camera("cam-b", self.building_b)
        self.cam_inactive_location = self.camera("cam-inactive-location", self.inactive_child)
        self.cam_inactive = self.camera("cam-inactive", self.faculty_a, is_active=False)
        self.cam_legacy_a = self.camera("cam-legacy-a", None, faculty="faculty-a")
        self.cam_legacy_b = self.camera("cam-legacy-b", None, faculty="faculty-b")

        self.unit_a = SecurityUnit.objects.create(name="Unit A", code="unit-a", unit_type="faculty")
        self.coverage_a = SecurityUnitCoverage.objects.create(
            security_unit=self.unit_a,
            location=self.faculty_a,
            include_descendants=True,
        )
        self.unit_b = SecurityUnit.objects.create(name="Unit B", code="unit-b", unit_type="faculty")
        self.coverage_b = SecurityUnitCoverage.objects.create(
            security_unit=self.unit_b,
            location=self.faculty_b,
            include_descendants=True,
        )
        self.user = self.approved_user("operator-a", role="operator")
        self.assignment_a = UserSecurityAssignment.objects.create(
            user=self.user,
            security_unit=self.unit_a,
            role_in_unit="guard",
        )

    @staticmethod
    def camera(camera_id, location, faculty=None, is_active=True):
        return Camera.objects.create(
            name=camera_id,
            camera_id=camera_id,
            source="0",
            location=location,
            faculty=faculty,
            is_active=is_active,
            use_speed_detection=True,
        )

    @staticmethod
    def approved_user(username, role="viewer", **kwargs):
        user = User.objects.create_user(username=username, password="test-pass", **kwargs)
        user.profile.status = "approved"
        user.profile.role = role
        user.profile.save()
        return user

    def camera_ids(self, user=None, **kwargs):
        user = user or self.user
        return set(get_user_accessible_cameras(user, **kwargs).values_list("camera_id", flat=True))

    def test_11_active_assignment_returns_unit(self):
        self.assertEqual(list(get_user_security_units(self.user)), [self.unit_a])

    def test_12_subtree_coverage_includes_descendants(self):
        self.assertIn(self.room_a, get_user_location_scope(self.user))
        self.assertIn("cam-a-child", self.camera_ids())

    def test_13_unrelated_branch_is_excluded(self):
        self.assertNotIn("cam-b", self.camera_ids())

    def test_14_exact_coverage_excludes_descendants(self):
        self.coverage_a.include_descendants = False
        self.coverage_a.save()
        ids = self.camera_ids()
        self.assertIn("cam-a", ids)
        self.assertNotIn("cam-a-child", ids)

    def test_15_multiple_units_are_unioned(self):
        UserSecurityAssignment.objects.create(user=self.user, security_unit=self.unit_b)
        ids = self.camera_ids()
        self.assertIn("cam-a", ids)
        self.assertIn("cam-b", ids)

    def test_16_overlapping_coverages_do_not_duplicate_cameras(self):
        child_unit = SecurityUnit.objects.create(name="Child Unit", code="child-unit")
        SecurityUnitCoverage.objects.create(
            security_unit=child_unit, location=self.building_a, include_descendants=True
        )
        UserSecurityAssignment.objects.create(user=self.user, security_unit=child_unit)
        ids = list(get_user_accessible_cameras(self.user).values_list("camera_id", flat=True))
        self.assertEqual(ids.count("cam-a-child"), 1)

    def test_17_inactive_assignment_removes_scope(self):
        self.assignment_a.active = False
        self.assignment_a.save()
        self.assertNotIn("cam-a", self.camera_ids())

    def test_18_inactive_unit_removes_scope(self):
        self.unit_a.active = False
        self.unit_a.save()
        self.assertNotIn("cam-a", self.camera_ids())

    def test_19_inactive_coverage_removes_scope(self):
        self.coverage_a.active = False
        self.coverage_a.save()
        self.assertNotIn("cam-a", self.camera_ids())

    def test_20_inactive_location_is_excluded(self):
        self.assertNotIn("cam-inactive-location", self.camera_ids())

    def test_21_inactive_ancestor_blocks_active_child(self):
        self.assertNotIn(self.inactive_child, get_user_location_scope(self.user))

    def test_22_active_only_excludes_inactive_camera(self):
        self.assertNotIn("cam-inactive", self.camera_ids(active_only=True))

    def test_23_inactive_profile_has_no_scope(self):
        self.user.profile.status = "pending"
        self.user.profile.save()
        self.assertFalse(self.camera_ids())

    def test_24_legacy_faculty_fallback_is_preserved(self):
        legacy_user = self.approved_user("legacy")
        legacy_user.profile.faculty = "faculty-b"
        legacy_user.profile.save()
        self.assertEqual(self.camera_ids(legacy_user), {"cam-legacy-b"})

    def test_25_scope_codes_cover_unlinked_legacy_camera(self):
        self.assertIn("cam-legacy-a", self.camera_ids())

    def test_26_location_is_authoritative_over_legacy_faculty(self):
        self.cam_b.faculty = "faculty-a"
        self.cam_b.save()
        self.assertNotIn("cam-b", self.camera_ids())

    def test_27_superuser_has_full_bypass(self):
        admin = self.approved_user("root", is_superuser=True)
        self.assertEqual(self.camera_ids(admin), set(Camera.objects.values_list("camera_id", flat=True)))

    def test_28_staff_has_full_bypass(self):
        staff = self.approved_user("staff", is_staff=True)
        self.assertIn("cam-b", self.camera_ids(staff))

    def test_29_approved_admin_role_has_full_bypass(self):
        admin = self.approved_user("it-admin", role="admin")
        self.assertIn("cam-b", self.camera_ids(admin))

    def test_30_viewer_can_receive_assignment_scope(self):
        viewer = self.approved_user("viewer", role="viewer")
        UserSecurityAssignment.objects.create(user=viewer, security_unit=self.unit_b)
        self.assertIn("cam-b", self.camera_ids(viewer))

    def test_31_duplicate_active_coverage_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SecurityUnitCoverage.objects.create(
                security_unit=self.unit_a,
                location=self.faculty_a,
                include_descendants=False,
            )

    def test_32_duplicate_active_assignment_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserSecurityAssignment.objects.create(user=self.user, security_unit=self.unit_a)

    def test_33_inactive_historical_duplicates_are_allowed(self):
        SecurityUnitCoverage.objects.create(
            security_unit=self.unit_a,
            location=self.faculty_a,
            active=False,
        )
        UserSecurityAssignment.objects.create(
            user=self.user,
            security_unit=self.unit_a,
            active=False,
        )

    def test_34_access_and_manage_helpers_differ(self):
        self.assertTrue(user_can_access_camera(self.user, self.cam_a_child))
        self.assertFalse(user_can_manage_camera(self.user, self.cam_a_child))
        admin = self.approved_user("manager", role="admin")
        self.assertTrue(user_can_manage_camera(admin, self.cam_a_child))

    def test_35_scope_query_count_is_bounded(self):
        fresh_user = User.objects.get(pk=self.user.pk)
        with CaptureQueriesContext(connection) as captured:
            list(get_user_accessible_cameras(fresh_user).values_list("camera_id", flat=True))
        self.assertLessEqual(len(captured), 7)

    def test_36_location_is_protected_by_camera(self):
        with self.assertRaises(ProtectedError):
            self.faculty_a.delete()


@override_settings(
    MIDDLEWARE=[
        item for item in settings.MIDDLEWARE
        if item != "whitenoise.middleware.WhiteNoiseMiddleware"
    ],
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ScopedViewTests(AccessScopeTests):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    @staticmethod
    def empty_report():
        return {
            "sources": [],
            "cameras": [],
            "recent_stage3": [],
            "recent_incidents": [],
            "recent_events": [],
            "recent_status": [],
        }

    def test_37_dashboard_lists_only_accessible_cameras(self):
        with patch("guvenlik.views._pipeline_report", return_value=self.empty_report()):
            response = self.client.get(reverse("dashboard:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cam-a")
        self.assertNotContains(response, "cam-b")

    def test_38_status_json_lists_only_accessible_cameras(self):
        with patch("guvenlik.views._pipeline_report", return_value=self.empty_report()):
            response = self.client.get(reverse("dashboard:status"))
        ids = {row["camera_id"] for row in response.json()["cameras"]}
        self.assertIn("cam-a", ids)
        self.assertNotIn("cam-b", ids)

    def test_39_fight_stream_idor_returns_404(self):
        response = self.client.get(reverse("dashboard:stream", args=["cam-b"]))
        self.assertEqual(response.status_code, 404)

    def test_40_fight_preview_idor_returns_404(self):
        response = self.client.get(reverse("dashboard:preview_image", args=["cam-b"]))
        self.assertEqual(response.status_code, 404)

    def test_41_incident_clip_idor_returns_404(self):
        report = self.empty_report()
        report["recent_incidents"] = [{
            "run_name": "foreign-run",
            "camera_id": "cam-b",
            "clip_name": "foreign.mp4",
        }]
        with patch("guvenlik.views._pipeline_report", return_value=report):
            response = self.client.get(
                reverse("dashboard:incident_video", args=["foreign-run", "foreign.mp4"])
            )
        self.assertEqual(response.status_code, 404)

    def test_42_speed_stream_idor_returns_404(self):
        SpeedCameraConfig.objects.create(camera=self.cam_b, enabled=True)
        response = self.client.get(reverse("speed_detection:camera_stream", args=["cam-b"]))
        self.assertEqual(response.status_code, 404)

    def test_43_speed_preview_idor_returns_404(self):
        response = self.client.get(reverse("speed_detection:preview", args=["run-x", "cam-b"]))
        self.assertEqual(response.status_code, 404)

    def test_43b_superuser_bypasses_profile_approval_on_admin_view(self):
        superuser = User.objects.create_superuser(
            username="pending-superuser",
            password="test-pass",
            email="root@example.test",
        )
        self.assertEqual(superuser.profile.status, "pending")
        self.client.force_login(superuser)
        response = self.client.get(reverse("adminx:camera_list"))
        self.assertEqual(response.status_code, 200)


class CompatibilityAndSeedTests(TestCase):
    def test_44_camera_id_and_runtime_source_are_unchanged(self):
        camera = Camera.objects.create(name="Legacy", camera_id="legacy-001", source="rtsp://example/test")
        self.assertEqual(camera.camera_id, "legacy-001")
        self.assertEqual(camera.get_runtime_source(), "rtsp://example/test")
        self.assertIsNone(camera.location_id)

    def test_45_speed_one_to_one_still_uses_camera(self):
        camera = Camera.objects.create(name="Speed", camera_id="speed-001", source="0")
        config = SpeedCameraConfig.objects.create(camera=camera, enabled=True)
        self.assertEqual(config.camera.camera_id, "speed-001")
        self.assertEqual(camera.speed_config.pk, config.pk)

    def test_46_camera_form_writes_location_and_legacy_bridge(self):
        location = Location.objects.create(name="Entrance", code="main-entrance", location_type="entrance")
        form = CameraForm(data={
            "name": "Entrance Camera",
            "camera_id": "entrance-001",
            "source_mode": "manual",
            "source": "0",
            "description": "",
            "location": location.pk,
            "faculty": "",
            "is_active": True,
            "use_fight_detection": True,
            "use_speed_detection": False,
        })
        self.assertTrue(form.is_valid(), form.errors)
        camera = form.save()
        self.assertEqual(camera.location, location)
        self.assertEqual(camera.faculty, location.code)

    def test_47_seed_command_is_idempotent(self):
        call_command("seed_security_structure", stdout=StringIO())
        first = (
            Location.objects.filter(code__startswith="demo-").count(),
            SecurityUnit.objects.filter(code="central-security").count(),
            SecurityUnitCoverage.objects.filter(
                security_unit__code="central-security", active=True
            ).count(),
        )
        call_command("seed_security_structure", stdout=StringIO())
        second = (
            Location.objects.filter(code__startswith="demo-").count(),
            SecurityUnit.objects.filter(code="central-security").count(),
            SecurityUnitCoverage.objects.filter(
                security_unit__code="central-security", active=True
            ).count(),
        )
        self.assertEqual(first, second)

    def test_48_unit_location_is_protected(self):
        location = Location.objects.create(name="HQ", code="hq", location_type="building")
        SecurityUnit.objects.create(name="HQ Unit", code="hq-unit", location=location)
        with self.assertRaises(ProtectedError):
            location.delete()


class CentralizedFightPreviewTests(TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="preview-tests-"))
        self.media_root = self.temp_dir / "media"
        self.run_dir = self.media_root / "pipeline_runs" / "active-run"
        self.preview = self.run_dir / "previews" / "cam-preview.jpg"
        self.preview.parent.mkdir(parents=True)
        self.preview.write_bytes(b"jpeg-frame")
        self.settings_override = self.settings(
            MEDIA_ROOT=self.media_root,
            PIPELINE_OUTPUT_BASE=self.media_root / "pipeline_runs",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.user = User.objects.create_superuser(
            username="preview-admin",
            password="test-pass",
            email="preview@example.test",
        )
        self.camera = Camera.objects.create(
            name="Preview",
            camera_id="cam-preview",
            source="rtsp://physical-camera/live",
        )
        self.client.force_login(self.user)

    def active(self, run_id="run-1", run_dir=None, runtime_state="RUNNING"):
        return SimpleNamespace(
            run_id=run_id,
            run_dir=run_dir or self.run_dir,
            runtime_state=runtime_state,
            process=None,
            runtime_pid=1234,
            started_at=1.0,
        )

    def test_49_no_active_run_never_returns_historical_preview(self):
        from guvenlik.views import _find_preview_path

        with patch("guvenlik.views.get_active_run", return_value=None):
            self.assertIsNone(_find_preview_path(self.camera.camera_id))

    def test_50_active_preview_resolves_only_from_that_run(self):
        with patch("guvenlik.views.get_active_run", return_value=self.active()):
            from guvenlik.views import _find_preview_path

            self.assertEqual(_find_preview_path(self.camera.camera_id), self.preview.resolve())

    def test_51_stream_generator_terminates_when_runtime_stops(self):
        from guvenlik.views import _preview_file_mjpeg_generator

        active = self.active()
        with patch(
            "guvenlik.views.get_active_run",
            side_effect=[active, None],
        ), patch("guvenlik.views.PREVIEW_RUNTIME_CHECK_INTERVAL_SEC", 0.0):
            generator = _preview_file_mjpeg_generator(
                self.camera.camera_id,
                (active.run_id, str(self.run_dir.resolve())),
                self.preview.resolve(),
            )
            self.assertIn(b"jpeg-frame", next(generator))
            with self.assertRaises(StopIteration):
                next(generator)

    def test_52_stream_generator_terminates_when_active_run_changes(self):
        from guvenlik.views import _preview_file_mjpeg_generator

        active = self.active()
        changed = self.active(run_id="run-2", run_dir=self.run_dir.parent / "other-run")
        with patch(
            "guvenlik.views.get_active_run",
            side_effect=[active, changed],
        ), patch("guvenlik.views.PREVIEW_RUNTIME_CHECK_INTERVAL_SEC", 0.0):
            generator = _preview_file_mjpeg_generator(
                self.camera.camera_id,
                (active.run_id, str(self.run_dir.resolve())),
                self.preview.resolve(),
            )
            next(generator)
            with self.assertRaises(StopIteration):
                next(generator)

    def test_53_django_stream_never_opens_physical_camera(self):
        from guvenlik.views import stream

        request = RequestFactory().get(
            reverse("dashboard:stream", args=[self.camera.camera_id])
        )
        request.user = self.user
        with patch("guvenlik.views.get_active_run", return_value=None), patch(
            "cv2.VideoCapture"
        ) as video_capture:
            with self.assertRaises(Http404):
                stream(request, self.camera.camera_id)
        video_capture.assert_not_called()

    def test_54_live_report_does_not_scan_historical_runs(self):
        from guvenlik.views import _empty_report, _pipeline_report

        active = self.active()
        active_report = _empty_report()
        active_report.update({"running": True, "run_name": "active-run"})
        with patch(
            "guvenlik.views.get_pipeline_status",
            return_value={
                "available": True,
                "runtime_state": "RUNNING",
                "supervisor_state": "RUNNING",
                "run_id": active.run_id,
                "runtime_exit_code": None,
            },
        ), patch("guvenlik.views.get_active_run", return_value=active), patch(
            "guvenlik.views._read_run_report", return_value=active_report
        ), patch("guvenlik.views._run_dirs") as run_dirs:
            report = _pipeline_report()
        run_dirs.assert_not_called()
        self.assertTrue(report["running"])
