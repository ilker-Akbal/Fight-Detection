from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from incidents.models import (
    Incident,
    IncidentAuditEvent,
    IncidentIngestCursor,
    IncidentIngestRecord,
)
from incidents.services.evidence import validate_evidence_path
from incidents.services.routing import process_due_routes, route_incident
from streams.models import Camera


REQUIRED_FIELDS = {
    "event_id",
    "run_id",
    "external_incident_id",
    "camera_id",
    "incident_type",
    "detected_at",
    "finalized_at",
}


def _parse_timestamp(value, field_name):
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        raise ValueError(f"invalid_{field_name}")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _safe_float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{getattr(stat, 'st_dev', 0)}:{getattr(stat, 'st_ino', 0)}"


def _invalid_record_id(source_identifier: str, offset: int, payload: bytes) -> str:
    digest = hashlib.sha256(
        source_identifier.encode("utf-8") + b":" + str(offset).encode("ascii") + b":" + payload
    ).hexdigest()
    return f"invalid-{digest[:64]}"


@transaction.atomic
def _record_malformed(source_identifier: str, offset: int, payload: bytes, error: str):
    event_id = _invalid_record_id(source_identifier, offset, payload)
    IncidentIngestRecord.objects.update_or_create(
        event_id=event_id,
        defaults={
            "source_identifier": source_identifier,
            "byte_offset": offset,
            "raw_envelope": {},
            "status": IncidentIngestRecord.STATUS_INVALID,
            "error_code": "malformed_jsonl",
            "error_message": str(error)[:500],
            "attempts": 1,
        },
    )


def _validate_envelope(envelope: dict) -> uuid.UUID:
    if not isinstance(envelope, dict):
        raise ValueError("envelope_not_object")
    missing = sorted(REQUIRED_FIELDS - set(envelope))
    if missing:
        raise ValueError(f"missing_fields:{','.join(missing)}")
    event_id = uuid.UUID(str(envelope["event_id"]))
    incident_type = str(envelope.get("incident_type") or "").upper()
    if incident_type not in dict(Incident.TYPE_CHOICES):
        raise ValueError("invalid_incident_type")
    return event_id


@transaction.atomic
def ingest_envelope(
    envelope: dict,
    *,
    source_identifier: str,
    byte_offset: int,
    existing_record: IncidentIngestRecord | None = None,
) -> tuple[Incident | None, str]:
    event_uuid = _validate_envelope(envelope)
    event_id = str(event_uuid)

    record = existing_record
    if record is None:
        record, _ = IncidentIngestRecord.objects.get_or_create(
            event_id=event_id,
            defaults={
                "source_identifier": source_identifier,
                "byte_offset": byte_offset,
                "raw_envelope": envelope,
                "status": IncidentIngestRecord.STATUS_RETRYABLE,
                "attempts": 0,
            },
        )

    existing = Incident.objects.filter(event_id=event_uuid).first()
    if existing is None:
        existing = Incident.objects.filter(
            source_system=str(envelope.get("source_system") or "fight_runtime"),
            run_id=str(envelope["run_id"]),
            external_incident_id=str(envelope["external_incident_id"]),
        ).first()
    if existing is not None:
        record.status = (
            IncidentIngestRecord.STATUS_IMPORTED
            if record.incident_id == existing.pk
            else IncidentIngestRecord.STATUS_DUPLICATE
        )
        record.incident = existing
        record.error_code = ""
        record.error_message = ""
        record.attempts += 1
        record.save()
        return existing, record.status

    camera = Camera.objects.filter(camera_id=str(envelope["camera_id"])).first()
    if camera is None:
        record.status = IncidentIngestRecord.STATUS_RETRYABLE
        record.error_code = "unknown_camera"
        record.error_message = f"Camera not found: {str(envelope['camera_id'])[:180]}"
        record.attempts += 1
        record.raw_envelope = envelope
        record.save()
        return None, record.status

    evidence_path, evidence_valid, evidence_error = validate_evidence_path(
        str(envelope.get("evidence_path") or "")
    )
    now = timezone.now()
    incident = Incident.objects.create(
        event_id=event_uuid,
        source_system=str(envelope.get("source_system") or "fight_runtime")[:80],
        run_id=str(envelope["run_id"])[:100],
        external_incident_id=str(envelope["external_incident_id"])[:180],
        incident_type=str(envelope["incident_type"]).upper(),
        camera=camera,
        detected_at=_parse_timestamp(envelope["detected_at"], "detected_at"),
        finalized_at=_parse_timestamp(envelope["finalized_at"], "finalized_at"),
        ingested_at=now,
        routing_started_at=now,
        status=Incident.STATUS_OPEN,
        label=str(envelope.get("label") or "")[:80],
        decision_score=_safe_float(envelope.get("decision_score", envelope.get("confidence"))),
        max_score=_safe_float(envelope.get("max_score", envelope.get("confidence"))),
        mean_score=_safe_float(envelope.get("mean_score", envelope.get("confidence"))),
        part_count=_safe_int(envelope.get("part_count")),
        evidence_path=evidence_path,
        evidence_valid=evidence_valid,
        ingest_error=evidence_error,
    )
    record.status = IncidentIngestRecord.STATUS_IMPORTED
    record.error_code = ""
    record.error_message = ""
    record.incident = incident
    record.raw_envelope = envelope
    record.attempts += 1
    record.save()
    IncidentAuditEvent.objects.create(
        incident=incident,
        action=IncidentAuditEvent.ACTION_INGESTED,
        metadata={
            "ingest_record_id": record.pk,
            "evidence_valid": evidence_valid,
            "evidence_error": evidence_error,
        },
    )
    route_incident(incident, now=now)
    return incident, record.status


def retry_failed_records(limit: int = 100) -> int:
    records = list(
        IncidentIngestRecord.objects
        .filter(status=IncidentIngestRecord.STATUS_RETRYABLE)
        .order_by("updated_at", "pk")[:limit]
    )
    imported = 0
    for record in records:
        try:
            incident, status = ingest_envelope(
                record.raw_envelope,
                source_identifier=record.source_identifier,
                byte_offset=record.byte_offset,
                existing_record=record,
            )
            if incident is not None and status == IncidentIngestRecord.STATUS_IMPORTED:
                imported += 1
        except Exception as exc:
            record.attempts += 1
            record.error_code = "retry_failed"
            record.error_message = str(exc)[:500]
            record.save(update_fields=["attempts", "error_code", "error_message", "updated_at"])
    return imported


def consume_outbox(path: str | Path, *, max_records: int = 500) -> dict:
    source = Path(path).resolve()
    result = {"consumed": 0, "imported": 0, "invalid": 0, "retryable": 0}
    if not source.exists() or not source.is_file():
        return result

    source_identifier = str(source)
    identity = _file_identity(source)
    with transaction.atomic():
        cursor, _ = IncidentIngestCursor.objects.select_for_update().get_or_create(
            source_identifier=source_identifier
        )
        size = source.stat().st_size
        if (cursor.file_identity and cursor.file_identity != identity) or size < cursor.byte_offset:
            cursor.byte_offset = 0
            cursor.last_event_id = ""
        cursor.file_identity = identity
        cursor.save()
        start_offset = cursor.byte_offset

    with source.open("rb") as handle:
        handle.seek(start_offset)
        while result["consumed"] < max_records:
            line_offset = handle.tell()
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            next_offset = handle.tell()

            event_id = ""
            try:
                envelope = json.loads(line.decode("utf-8"))
                incident, status = ingest_envelope(
                    envelope,
                    source_identifier=source_identifier,
                    byte_offset=line_offset,
                )
                event_id = str(envelope.get("event_id") or "")
                if incident is not None:
                    result["imported"] += 1
                elif status == IncidentIngestRecord.STATUS_RETRYABLE:
                    result["retryable"] += 1
            except Exception as exc:
                _record_malformed(source_identifier, line_offset, line, str(exc))
                result["invalid"] += 1

            with transaction.atomic():
                cursor = IncidentIngestCursor.objects.select_for_update().get(
                    source_identifier=source_identifier
                )
                if cursor.byte_offset != line_offset:
                    break
                cursor.byte_offset = next_offset
                cursor.last_event_id = event_id[:100]
                cursor.file_identity = identity
                cursor.save()
            result["consumed"] += 1
    return result


def dispatcher_tick(path: str | Path) -> dict:
    result = consume_outbox(path)
    result["retried"] = retry_failed_records()
    result["routes_created"] = process_due_routes()
    return result
