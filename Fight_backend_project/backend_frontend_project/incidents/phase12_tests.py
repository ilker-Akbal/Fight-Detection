"""Focused ORM integration test, run by the general suite in isolated SQLite."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from fight.operations import atomic_json
from incidents.models import Incident, IncidentIngestCursor, IncidentIngestRecord
from incidents.services.ingest import _file_identity
from incidents.services.retention import cleanup_tick, outbox_consumed
from streams.models import Camera


class RetentionIntegrationTests(TestCase):
    def test_orm_reference_and_cursor_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            runs = media / "pipeline_runs"
            old = runs / "old"
            evidence = old / "incidents" / "protected.mp4"
            orphan = old / "incidents" / "orphan.mp4"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(b"evidence")
            orphan.write_bytes(b"orphan")
            for path in (evidence, orphan):
                os.utime(path, (1, 1))
            atomic_json(old / ".run_state.json", {"state": "COMPLETED", "run_id": "old"})
            outbox = media / "outbox.jsonl"
            outbox.write_bytes(b"{}\n")
            IncidentIngestCursor.objects.create(source_identifier=str(outbox.resolve()),
                                               file_identity=_file_identity(outbox), byte_offset=3)
            camera = Camera.objects.create(name="Camera", camera_id="retention-camera", source="0")
            Incident.objects.create(camera=camera, run_id="old", external_incident_id="one",
                                    detected_at=timezone.now(), finalized_at=timezone.now(),
                                    status=Incident.STATUS_RESOLVED, evidence_valid=False,
                                    evidence_path=evidence.relative_to(media).as_posix())
            state_dir = root / "supervisor"
            log = state_dir / "logs" / "runtime-old.stdout.log"
            log.parent.mkdir(parents=True)
            log.write_bytes(b"closed log")
            os.utime(log, (1, 1))
            atomic_json(state_dir / "runtime_state.json", {
                "runtime_state": "STOPPED", "runtime_pid": None, "run_id": "current",
                "health_snapshot_path": str(runs / "current" / "runtime_health.json"),
            })
            with override_settings(MEDIA_ROOT=media, PIPELINE_OUTPUT_BASE=runs,
                                   OPERATIONAL_SERVICE_DIR=root / "services",
                                   INCIDENT_OUTBOX_PATH=outbox,
                                   OPERATIONAL_RETENTION={"evidence_days": 180}), patch.dict(
                                       os.environ, {"RUNTIME_SUPERVISOR_STATE_DIR": str(state_dir)}):
                assert outbox_consumed(outbox)
                result = cleanup_tick()
                assert result["removed"] == 2 and not log.exists()
                assert evidence.exists() and not orphan.exists()
                # Partial trailing data and retryable records both fail closed.
                with outbox.open("ab") as handle:
                    handle.write(b'{"partial":')
                assert not outbox_consumed(outbox)
                outbox.write_bytes(b"{}\n")
                IncidentIngestRecord.objects.create(event_id="retry", byte_offset=0,
                                                    status=IncidentIngestRecord.STATUS_RETRYABLE)
                assert not outbox_consumed(outbox)
