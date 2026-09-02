import json
import shutil
import tempfile
import threading
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from adminx.models import (
    Location,
    SecurityUnit,
    SecurityUnitCoverage,
    UserSecurityAssignment,
)
from incidents.models import (
    Incident,
    IncidentAuditEvent,
    IncidentIngestCursor,
    IncidentIngestRecord,
    IncidentRoute,
    IncidentRoutingRule,
)
from incidents.services.actions import acknowledge_incident, resolve_incident
from incidents.services.ingest import consume_outbox, dispatcher_tick
from incidents.services.routing import process_due_routes
from services.incident_access import (
    get_user_incident_inbox,
    user_can_ack_incident,
    user_can_view_incident,
)
from streams.models import Camera


class Phase8FixtureMixin:
    def setUp(self):
        super().setUp()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="phase8-tests-"))
        self.media_root = self.temp_dir / "media"
        self.evidence_root = self.media_root / "pipeline_runs"
        self.evidence_root.mkdir(parents=True)
        self.outbox = self.media_root / "runtime_spool" / "incidents_outbox.jsonl"
        self.settings_override = self.settings(
            MEDIA_ROOT=self.media_root,
            INCIDENT_EVIDENCE_ROOTS=[self.evidence_root],
            INCIDENT_OUTBOX_PATH=self.outbox,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

        self.campus = Location.objects.create(name="Campus", code="campus", location_type="campus")
        self.block = Location.objects.create(
            name="A Block", code="a-block", location_type="building", parent=self.campus
        )
        self.floor = Location.objects.create(
            name="Floor 1", code="floor-1", location_type="floor", parent=self.block
        )
        self.other = Location.objects.create(
            name="Other", code="other-building", location_type="building", parent=self.campus
        )
        self.camera = Camera.objects.create(
            name="A Camera", camera_id="cam-a", source="0", location=self.floor
        )
        self.other_camera = Camera.objects.create(
            name="Other Camera", camera_id="cam-other", source="1", location=self.other
        )

        self.block_unit = self.unit("Block Security", "block-security", self.block)
        self.dean_unit = self.unit("Deanery Security", "dean-security", self.campus)
        self.central_unit = self.unit(
            "Main Security", "main-security", self.campus, is_central=True
        )
        self.other_unit = self.unit("Other Security", "other-security", self.other)
        self.coverage(self.block_unit, self.block, True)
        self.coverage(self.dean_unit, self.campus, True)
        self.coverage(self.central_unit, self.campus, True)
        self.coverage(self.other_unit, self.other, True)

        self.rule0 = self.rule(self.block_unit, 0, 0)
        self.rule1 = self.rule(self.dean_unit, 1, 30)
        self.rule2 = self.rule(self.central_unit, 2, 60)

        self.block_user = self.user("block-user", self.block_unit)
        self.dean_user = self.user("dean-user", self.dean_unit)
        self.central_user = self.user("central-user", self.central_unit)
        self.other_user = self.user("other-user", self.other_unit)

    @staticmethod
    def unit(name, code, location, is_central=False):
        return SecurityUnit.objects.create(
            name=name,
            code=code,
            location=location,
            active=True,
            is_central=is_central,
        )

    @staticmethod
    def coverage(unit, location, descendants):
        return SecurityUnitCoverage.objects.create(
            security_unit=unit,
            location=location,
            include_descendants=descendants,
            active=True,
        )

    @staticmethod
    def rule(unit, stage, delay):
        return IncidentRoutingRule.objects.create(
            name=f"{unit.code}-{stage}",
            security_unit=unit,
            incident_type=Incident.TYPE_FIGHT,
            routing_stage=stage,
            delay_sec=delay,
            priority=stage,
        )

    @staticmethod
    def user(username, unit):
        user = User.objects.create_user(username=username, password="test-pass")
        user.profile.status = "approved"
        user.profile.role = "operator"
        user.profile.save()
        UserSecurityAssignment.objects.create(user=user, security_unit=unit)
        return user

    def evidence(self, run="run-1", name="incident.mp4"):
        path = self.evidence_root / run / "incidents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"evidence")
        return path

    def envelope(self, **values):
        evidence = values.pop("evidence_path", self.evidence())
        base = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "source_system": "fight_runtime",
            "run_id": "run-1",
            "external_incident_id": f"cam-a_incident_{uuid.uuid4().hex[:8]}",
            "camera_id": "cam-a",
            "incident_type": "FIGHT",
            "detected_at": "2026-09-02T10:00:00+00:00",
            "finalized_at": "2026-09-02T10:00:03+00:00",
            "label": "fight",
            "decision_score": 0.91,
            "max_score": 0.96,
            "mean_score": 0.87,
            "confidence": 0.91,
            "part_count": 2,
            "evidence_path": str(evidence),
            "created_wall_time": 1788343203.0,
        }
        base.update(values)
        return base

    def append(self, envelope, newline=True):
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox.open("ab") as handle:
            handle.write(json.dumps(envelope).encode("utf-8"))
            if newline:
                handle.write(b"\n")

    def ingest(self, envelope=None):
        envelope = envelope or self.envelope()
        self.append(envelope)
        consume_outbox(self.outbox)
        return Incident.objects.get(event_id=envelope["event_id"])


class IncidentIngestTests(Phase8FixtureMixin, TestCase):
    def test_01_camera_id_maps_to_existing_camera(self):
        incident = self.ingest()
        self.assertEqual(incident.camera, self.camera)

    def test_02_run_id_is_preserved(self):
        incident = self.ingest(self.envelope(run_id="run-special"))
        self.assertEqual(incident.run_id, "run-special")

    def test_03_same_event_id_is_idempotent(self):
        envelope = self.envelope()
        self.append(envelope)
        self.append(envelope)
        consume_outbox(self.outbox)
        self.assertEqual(Incident.objects.filter(event_id=envelope["event_id"]).count(), 1)

    def test_04_same_source_run_external_identity_is_idempotent(self):
        first = self.envelope(external_incident_id="same")
        second = self.envelope(external_incident_id="same")
        self.append(first)
        self.append(second)
        consume_outbox(self.outbox)
        self.assertEqual(Incident.objects.filter(run_id="run-1", external_incident_id="same").count(), 1)

    def test_05_same_external_id_in_different_runs_is_supported(self):
        self.append(self.envelope(run_id="run-a", external_incident_id="same"))
        self.append(self.envelope(run_id="run-b", external_incident_id="same"))
        consume_outbox(self.outbox)
        self.assertEqual(Incident.objects.filter(external_incident_id="same").count(), 2)

    def test_06_unknown_camera_is_retryable_and_not_mapped(self):
        envelope = self.envelope(camera_id="missing-camera")
        self.append(envelope)
        consume_outbox(self.outbox)
        record = IncidentIngestRecord.objects.get(event_id=envelope["event_id"])
        self.assertEqual(record.status, IncidentIngestRecord.STATUS_RETRYABLE)
        self.assertEqual(record.error_code, "unknown_camera")
        self.assertFalse(Incident.objects.filter(event_id=envelope["event_id"]).exists())

    def test_07_unknown_camera_record_imports_after_camera_appears(self):
        envelope = self.envelope(camera_id="late-camera")
        self.append(envelope)
        consume_outbox(self.outbox)
        Camera.objects.create(name="Late", camera_id="late-camera", source="2", location=self.floor)
        dispatcher_tick(self.outbox)
        self.assertTrue(Incident.objects.filter(event_id=envelope["event_id"]).exists())

    def test_08_path_traversal_is_rejected_without_dropping_incident(self):
        incident = self.ingest(self.envelope(evidence_path="../../secret.mp4"))
        self.assertFalse(incident.evidence_valid)
        self.assertEqual(incident.evidence_path, "")
        self.assertEqual(incident.ingest_error, "evidence_path_outside_allowed_roots")

    def test_09_valid_evidence_is_stored_as_media_relative_path(self):
        incident = self.ingest()
        self.assertTrue(incident.evidence_valid)
        self.assertFalse(Path(incident.evidence_path).is_absolute())

    def test_10_partial_line_does_not_advance_cursor(self):
        self.append(self.envelope(), newline=False)
        result = consume_outbox(self.outbox)
        cursor = IncidentIngestCursor.objects.get()
        self.assertEqual(result["consumed"], 0)
        self.assertEqual(cursor.byte_offset, 0)

    def test_11_partial_line_imports_after_newline_arrives(self):
        envelope = self.envelope()
        self.append(envelope, newline=False)
        consume_outbox(self.outbox)
        with self.outbox.open("ab") as handle:
            handle.write(b"\n")
        consume_outbox(self.outbox)
        self.assertTrue(Incident.objects.filter(event_id=envelope["event_id"]).exists())

    def test_12_malformed_complete_line_is_recorded_and_skipped(self):
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        self.outbox.write_bytes(b"{broken}\n")
        result = consume_outbox(self.outbox)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(IncidentIngestRecord.objects.get().status, IncidentIngestRecord.STATUS_INVALID)
        self.assertEqual(IncidentIngestCursor.objects.get().byte_offset, len(b"{broken}\n"))

    def test_13_malformed_line_does_not_block_following_valid_line(self):
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        self.outbox.write_bytes(b"bad\n")
        envelope = self.envelope()
        self.append(envelope)
        consume_outbox(self.outbox)
        self.assertTrue(Incident.objects.filter(event_id=envelope["event_id"]).exists())

    def test_14_cursor_restart_continues_at_saved_offset(self):
        first = self.envelope()
        self.append(first)
        consume_outbox(self.outbox)
        offset = IncidentIngestCursor.objects.get().byte_offset
        second = self.envelope()
        self.append(second)
        consume_outbox(self.outbox)
        self.assertGreater(IncidentIngestCursor.objects.get().byte_offset, offset)
        self.assertEqual(Incident.objects.count(), 2)

    def test_15_truncation_resets_cursor_safely(self):
        self.ingest()
        self.outbox.write_bytes(b"")
        consume_outbox(self.outbox)
        second = self.envelope(run_id="run-2")
        self.append(second)
        consume_outbox(self.outbox)
        self.assertTrue(Incident.objects.filter(event_id=second["event_id"]).exists())

    def test_16_ingested_audit_is_created(self):
        incident = self.ingest()
        audit = incident.audit_events.get(action=IncidentAuditEvent.ACTION_INGESTED)
        self.assertTrue(audit.metadata["evidence_valid"])

    def test_17_dispatcher_once_command_consumes_outbox(self):
        envelope = self.envelope()
        self.append(envelope)
        call_command("run_incident_dispatcher", "--once", "--outbox", str(self.outbox), stdout=StringIO())
        self.assertTrue(Incident.objects.filter(event_id=envelope["event_id"]).exists())

    def test_18_dispatcher_restart_does_not_duplicate_route(self):
        incident = self.ingest()
        dispatcher_tick(self.outbox)
        dispatcher_tick(self.outbox)
        self.assertEqual(IncidentRoute.objects.filter(incident=incident, security_unit=self.block_unit).count(), 1)


class IncidentRoutingTests(Phase8FixtureMixin, TestCase):
    def test_19_stage0_routes_to_block_unit(self):
        incident = self.ingest()
        self.assertTrue(incident.routes.filter(security_unit=self.block_unit, routing_stage=0).exists())

    def test_20_descendant_camera_is_covered(self):
        incident = self.ingest()
        self.assertEqual(incident.camera.location, self.floor)
        self.assertTrue(incident.routes.filter(security_unit=self.block_unit).exists())

    def test_21_unrelated_unit_does_not_receive_route(self):
        incident = self.ingest()
        self.assertFalse(incident.routes.filter(security_unit=self.other_unit).exists())

    def test_22_overlapping_coverage_routes_each_due_rule(self):
        incident = self.ingest()
        due = incident.routing_started_at + timedelta(seconds=61)
        process_due_routes(now=due)
        units = set(incident.routes.values_list("security_unit_id", flat=True))
        self.assertEqual(units, {self.block_unit.pk, self.dean_unit.pk, self.central_unit.pk})

    def test_23_stage1_not_created_before_delay(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=29))
        self.assertFalse(incident.routes.filter(routing_stage=1).exists())

    def test_24_stage1_created_when_absolute_delay_is_due(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=30))
        self.assertTrue(incident.routes.filter(routing_stage=1, security_unit=self.dean_unit).exists())

    def test_25_stage2_created_when_absolute_delay_is_due(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=60))
        self.assertTrue(incident.routes.filter(routing_stage=2, security_unit=self.central_unit).exists())

    def test_26_duplicate_due_tick_does_not_duplicate_routes(self):
        incident = self.ingest()
        due = incident.routing_started_at + timedelta(seconds=90)
        process_due_routes(now=due)
        process_due_routes(now=due)
        self.assertEqual(incident.routes.count(), 3)

    def test_27_no_rule_and_no_central_fallback_is_unrouted(self):
        IncidentRoutingRule.objects.update(active=False)
        self.central_unit.active = False
        self.central_unit.save()
        incident = self.ingest()
        incident.refresh_from_db()
        self.assertEqual(incident.routing_state, Incident.ROUTING_UNROUTED)

    def test_28_central_coverage_is_explicit_fallback(self):
        IncidentRoutingRule.objects.update(active=False)
        incident = self.ingest()
        self.assertTrue(incident.routes.filter(security_unit=self.central_unit, reason="central_fallback").exists())

    def test_29_inactive_rule_does_not_route(self):
        self.rule0.active = False
        self.rule0.save()
        incident = self.ingest()
        self.assertFalse(incident.routes.filter(security_unit=self.block_unit).exists())

    def test_30_inactive_unit_does_not_route(self):
        self.block_unit.active = False
        self.block_unit.save()
        incident = self.ingest()
        self.assertFalse(incident.routes.filter(security_unit=self.block_unit).exists())

    def test_31_inactive_coverage_does_not_route(self):
        SecurityUnitCoverage.objects.filter(security_unit=self.block_unit).update(active=False)
        incident = self.ingest()
        self.assertFalse(incident.routes.filter(security_unit=self.block_unit).exists())

    def test_32_acknowledged_incident_does_not_escalate(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=90))
        self.assertEqual(incident.routes.count(), 1)

    def test_33_resolved_incident_does_not_escalate(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        resolve_incident(self.block_user, incident, "Handled")
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=90))
        self.assertEqual(incident.routes.count(), 1)

    def test_34_routed_audit_is_created(self):
        incident = self.ingest()
        self.assertTrue(incident.audit_events.filter(action=IncidentAuditEvent.ACTION_ROUTED).exists())

    def test_35_escalated_audit_is_created(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=31))
        audit = incident.audit_events.get(action=IncidentAuditEvent.ACTION_ESCALATED)
        self.assertEqual(audit.security_unit, self.dean_unit)
        self.assertEqual(audit.routing_stage, 1)

    def test_36_unrouted_audit_is_created_once(self):
        IncidentRoutingRule.objects.update(active=False)
        self.central_unit.active = False
        self.central_unit.save()
        incident = self.ingest()
        process_due_routes(now=timezone.now() + timedelta(seconds=100))
        self.assertEqual(incident.audit_events.filter(action=IncidentAuditEvent.ACTION_ROUTING_FAILED).count(), 1)

    def test_36b_legacy_faculty_code_routes_only_when_location_resolves(self):
        legacy = Camera.objects.create(
            name="Legacy Camera",
            camera_id="legacy-cam",
            source="3",
            faculty=self.floor.code,
            location=None,
        )
        incident = self.ingest(self.envelope(camera_id=legacy.camera_id))
        self.assertTrue(incident.routes.filter(security_unit=self.block_unit).exists())

    def test_36c_missing_location_is_explicitly_unrouted(self):
        camera = Camera.objects.create(
            name="Unmapped Camera",
            camera_id="unmapped-cam",
            source="4",
            faculty="unknown-location-code",
            location=None,
        )
        incident = self.ingest(self.envelope(camera_id=camera.camera_id))
        incident.refresh_from_db()
        self.assertEqual(incident.routing_state, Incident.ROUTING_UNROUTED)


class IncidentActionTests(Phase8FixtureMixin, TestCase):
    def test_37_routed_unit_user_can_ack(self):
        incident = self.ingest()
        result = acknowledge_incident(self.block_user, incident)
        self.assertEqual(result["result"], "acknowledged")

    def test_38_unrelated_unit_user_cannot_ack(self):
        incident = self.ingest()
        with self.assertRaises(PermissionDenied):
            acknowledge_incident(self.other_user, incident)

    def test_39_inactive_assignment_cannot_ack(self):
        UserSecurityAssignment.objects.filter(user=self.block_user).update(active=False)
        incident = self.ingest()
        with self.assertRaises(PermissionDenied):
            acknowledge_incident(self.block_user, incident)

    def test_40_first_ack_wins(self):
        incident = self.ingest()
        first = acknowledge_incident(self.block_user, incident)
        second = acknowledge_incident(self.block_user, incident)
        self.assertEqual(first["result"], "acknowledged")
        self.assertEqual(second["result"], "already_acknowledged")
        self.assertEqual(incident.audit_events.filter(action=IncidentAuditEvent.ACTION_ACKNOWLEDGED).count(), 1)

    def test_41_ack_keeps_existing_routes(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=61))
        count = incident.routes.count()
        acknowledge_incident(self.block_user, incident)
        self.assertEqual(incident.routes.count(), count)

    def test_42_ack_audit_keeps_actor_and_unit(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        audit = incident.audit_events.get(action=IncidentAuditEvent.ACTION_ACKNOWLEDGED)
        self.assertEqual(audit.actor, self.block_user)
        self.assertEqual(audit.security_unit, self.block_unit)

    def test_43_acknowledging_unit_can_resolve(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        result = resolve_incident(self.block_user, incident, "Area checked")
        self.assertEqual(result["result"], "resolved")

    def test_44_unrelated_user_cannot_resolve(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        with self.assertRaises(PermissionDenied):
            resolve_incident(self.other_user, incident, "No")

    def test_45_resolution_note_is_required(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        with self.assertRaises(ValidationError):
            resolve_incident(self.block_user, incident, "")

    def test_46_resolution_note_and_audit_are_stored(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        resolve_incident(self.block_user, incident, "Resolved safely")
        incident.refresh_from_db()
        self.assertEqual(incident.resolution_note, "Resolved safely")
        self.assertTrue(incident.audit_events.filter(action=IncidentAuditEvent.ACTION_RESOLVED, actor=self.block_user).exists())

    def test_47_admin_can_ack_unrouted_incident(self):
        IncidentRoutingRule.objects.update(active=False)
        SecurityUnit.objects.update(active=False)
        incident = self.ingest()
        admin = User.objects.create_superuser("root", "root@example.test", "test-pass")
        result = acknowledge_incident(admin, incident)
        self.assertEqual(result["result"], "acknowledged")

    def test_48_camera_view_access_alone_does_not_grant_ack(self):
        view_unit = self.unit("View Scope", "view-scope", self.floor)
        self.coverage(view_unit, self.floor, False)
        viewer = self.user("scope-viewer", view_unit)
        incident = self.ingest()
        self.assertTrue(user_can_view_incident(viewer, incident))
        self.assertFalse(user_can_ack_incident(viewer, incident))

    def test_48b_routed_central_security_can_resolve_acknowledged_incident(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=61))
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        result = resolve_incident(self.central_user, incident, "Central review complete")
        self.assertEqual(result["result"], "resolved")


@override_settings(
    MIDDLEWARE=[item for item in settings.MIDDLEWARE if item != "whitenoise.middleware.WhiteNoiseMiddleware"],
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class IncidentViewTests(Phase8FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_49_user_inbox_contains_only_assigned_routes(self):
        incident = self.ingest()
        self.assertEqual(list(get_user_incident_inbox(self.block_user).values_list("incident_id", flat=True)), [incident.pk])
        self.assertFalse(get_user_incident_inbox(self.other_user).filter(incident=incident).exists())

    def test_50_main_security_receives_due_escalation(self):
        incident = self.ingest()
        process_due_routes(now=incident.routing_started_at + timedelta(seconds=61))
        self.assertTrue(get_user_incident_inbox(self.central_user).filter(incident=incident).exists())

    def test_51_ack_idor_is_denied(self):
        incident = self.ingest()
        self.client.force_login(self.other_user)
        response = self.client.post(reverse("dashboard:incident_ack", args=[incident.pk]))
        self.assertEqual(response.status_code, 403)

    def test_52_ack_endpoint_rejects_get(self):
        incident = self.ingest()
        self.client.force_login(self.block_user)
        self.assertEqual(self.client.get(reverse("dashboard:incident_ack", args=[incident.pk])).status_code, 405)

    def test_53_authorized_ack_endpoint_updates_incident(self):
        incident = self.ingest()
        self.client.force_login(self.block_user)
        response = self.client.post(
            reverse("dashboard:incident_ack", args=[incident.pk]),
            {"security_unit_id": self.block_unit.pk},
        )
        incident.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(incident.status, Incident.STATUS_ACKNOWLEDGED)

    def test_54_resolve_idor_is_denied(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        self.client.force_login(self.other_user)
        response = self.client.post(
            reverse("dashboard:incident_resolve", args=[incident.pk]),
            {"resolution_note": "attempt"},
        )
        self.assertEqual(response.status_code, 403)

    def test_55_evidence_idor_returns_404(self):
        incident = self.ingest()
        self.client.force_login(self.other_user)
        response = self.client.get(reverse("dashboard:incident_evidence", args=[incident.pk]))
        self.assertEqual(response.status_code, 404)

    def test_56_authorized_evidence_is_streamed(self):
        incident = self.ingest()
        self.client.force_login(self.block_user)
        response = self.client.get(reverse("dashboard:incident_evidence", args=[incident.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_57_superuser_evidence_bypass(self):
        incident = self.ingest()
        admin = User.objects.create_superuser("admin", "admin@example.test", "test-pass")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("dashboard:incident_evidence", args=[incident.pk])).status_code, 200)

    def test_58_dashboard_contains_route_backed_inbox(self):
        incident = self.ingest()
        self.client.force_login(self.block_user)
        response = self.client.get(reverse("dashboard:index"))
        self.assertContains(response, incident.external_incident_id)
        self.assertContains(response, self.block_unit.name)

    def test_59_sse_payload_uses_db_incident_identity(self):
        first = self.ingest(self.envelope(run_id="run-a"))
        second = self.ingest(self.envelope(run_id="run-b"))
        self.client.force_login(self.block_user)
        response = self.client.get(reverse("dashboard:events"))
        ids = {row["incident_id"] for row in response.json()["operational_incidents"]}
        self.assertEqual(ids, {first.pk, second.pk})

    def test_60_resolved_incident_leaves_active_inbox(self):
        incident = self.ingest()
        acknowledge_incident(self.block_user, incident)
        incident.refresh_from_db()
        resolve_incident(self.block_user, incident, "done")
        self.assertFalse(get_user_incident_inbox(self.block_user).filter(incident=incident).exists())


class IncidentConcurrencyTests(Phase8FixtureMixin, TransactionTestCase):
    reset_sequences = True

    def test_61_concurrent_ack_requests_create_one_acknowledgement(self):
        incident = self.ingest()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            close_old_connections()
            try:
                user = User.objects.get(pk=self.block_user.pk)
                current = Incident.objects.get(pk=incident.pk)
                barrier.wait(timeout=5)
                results.append(acknowledge_incident(user, current)["result"])
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertCountEqual(results, ["acknowledged", "already_acknowledged"])
        self.assertEqual(
            IncidentAuditEvent.objects.filter(
                incident=incident,
                action=IncidentAuditEvent.ACTION_ACKNOWLEDGED,
            ).count(),
            1,
        )
