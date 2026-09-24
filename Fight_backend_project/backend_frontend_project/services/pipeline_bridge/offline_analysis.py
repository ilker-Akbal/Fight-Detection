"""Django side of the one-shot job protocol; no model inference or source capture."""
import json
import uuid
from datetime import datetime, timezone as utc
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from fight.operations import atomic_json
from streams.models import OfflineAsset, OfflineRun, OfflineResult
from services.access_scope import get_user_location_scope, get_user_accessible_cameras, is_it_admin


def job_root():
    return Path(getattr(settings, "OFFLINE_JOB_DIR", Path(settings.REPO_ROOT) / ".runtime_supervisor" / "offline"))


def monitoring_held():
    return (job_root().parent / "monitoring_hold.json").exists()


def set_monitoring_hold(held):
    path = job_root().parent / "monitoring_hold.json"
    if held:
        atomic_json(path, {"held": True})
    else:
        path.unlink(missing_ok=True)


def accessible_assets(user):
    from django.db.models import Q
    rows = OfflineAsset.objects.select_related("location", "legacy_camera")
    if is_it_admin(user):
        return rows
    # Existing legacy camera scope remains the compatibility authority; new
    # uploads require explicit Location coverage, never a caller-supplied owner.
    return rows.filter(Q(legacy_camera__isnull=True, location__in=get_user_location_scope(user)) |
                       Q(legacy_camera__in=get_user_accessible_cameras(user)))


@transaction.atomic
def create_run(asset, analysis_type, configuration=None):
    OfflineAsset.objects.select_for_update().get(pk=asset.pk)
    if analysis_type not in {"FIGHT", "SPEED", "BOTH"}:
        raise ValueError("invalid_analysis_type")
    config = dict(configuration or {})
    json.dumps(config, allow_nan=False)
    if analysis_type in {"SPEED", "BOTH"}:
        from HizTespiti.speed.src.calibration_loader import load_calibration
        # Configuration is a validated document, not a client-selected path.
        calibration = config.pop("calibration", None)
        if not isinstance(calibration, dict):
            raise ValueError("speed_calibration_required")
        path = job_root() / "calibrations" / (uuid.uuid4().hex + ".json")
        atomic_json(path, calibration)
        try:
            if not load_calibration(path).ready:
                raise ValueError("speed_calibration_not_ready")
        except (ValueError, KeyError, TypeError, AttributeError):
            path.unlink(missing_ok=True)
            raise ValueError("speed_calibration_not_ready") from None
        config = {"calibration": calibration, "calibration_path": str(path),
                  "speed_limit_kmh": float(calibration.get("speed_limit_kmh", 30)),
                  "tolerance_kmh": float(calibration.get("tolerance_kmh", 5)),
                  "save_snapshot": True, "save_clip": True}
    else:
        config = {}
    return OfflineRun.objects.create(asset=asset, analysis_type=analysis_type, configuration=config)


def reconcile_jobs():
    """One outstanding request; interrupted claims are terminal, not requeued."""
    from incidents.models import IncidentIngestCursor
    root = job_root()
    for run in OfflineRun.objects.filter(state__in=["QUEUED", "PROCESSING"]).order_by("created_at")[:100]:
        path = root / (run.runtime_camera_id + ".json")
        if path.exists():
            row = json.loads(path.read_text(encoding="utf-8"))
            state = row["state"]
            if state == "COMPLETED":
                offset = IncidentIngestCursor.objects.filter(
                    source_identifier=str(Path(settings.INCIDENT_OUTBOX_PATH).resolve())
                ).values_list("byte_offset", flat=True).first() or 0
                if offset < row.get("outbox_offset", 0):
                    state = "PROCESSING"  # persisted detections not imported yet
            run.state = state
            run.started_at = datetime.fromtimestamp(row["started_at"], tz=utc.utc)
            run.error = row.get("error", "")
            if state in {"COMPLETED", "FAILED", "CANCELLED"}:
                run.completed_at = datetime.fromtimestamp(row["updated_at"], tz=utc.utc)
            run.save()
        elif run.cancel_requested:
            run.state, run.completed_at = "CANCELLED", timezone.now()
            run.save()
    run = OfflineRun.objects.filter(state__in=["QUEUED", "PROCESSING"]).order_by("created_at").first()
    if run is None:
        return False
    speed = {key: value for key, value in run.configuration.items() if key != "calibration"}
    request = {"cancel": run.cancel_requested, "camera": {
        "camera_id": run.runtime_camera_id, "name": run.asset.name, "source": run.asset.file_path,
        "enabled": True, "use_fight_detection": run.analysis_type in {"FIGHT", "BOTH"},
        "use_speed_detection": run.analysis_type in {"SPEED", "BOTH"}, "speed_config": speed}}
    atomic_json(root / "request.json", request)
    return True


def import_result(envelope, record):
    """Offline envelopes never create or route live Incident rows."""
    from incidents.services.evidence import validate_evidence_path
    cid = str(envelope["camera_id"])
    run = OfflineRun.objects.filter(pk=uuid.UUID(hex=cid[8:])).first()
    if run is None:
        record.status, record.error_code = "RETRYABLE", "unknown_offline_run"
    else:
        evidence, valid, _ = validate_evidence_path(envelope.get("evidence_path", ""))
        video_time = envelope.get("video_time_sec")
        if envelope["incident_type"] == "SPEED":
            video_time = envelope.get("speed", {}).get("timestamp_sec")
        OfflineResult.objects.get_or_create(event_id=envelope["event_id"], defaults={
            "run": run, "analysis_type": envelope["incident_type"], "video_time_sec": video_time,
            "confidence": envelope.get("confidence") if envelope["incident_type"] == "FIGHT" else None,
            "evidence_path": evidence if valid else "", "payload": envelope})
        record.status, record.error_code = "IMPORTED", ""
    record.attempts += 1
    record.raw_envelope = envelope
    record.save()
    return None, record.status
