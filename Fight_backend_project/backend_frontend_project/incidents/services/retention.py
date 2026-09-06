"""ORM-aware retention boundary. Runtime never imports this module."""
from contextlib import ExitStack
from pathlib import Path
import time

from django.conf import settings
from django.db.models import Q

from fight.operations import atomic_json
from fight.retention import RetentionPass, read_small_json
from fight.runtime_supervisor.core import SupervisorConfig, _pid_exists
from fight.runtime_supervisor.locking import SingletonLock, SingletonLockError
from incidents.models import Incident, IncidentIngestCursor, IncidentIngestRecord
from incidents.services.ingest import _file_identity


def outbox_consumed(path):
    path = Path(path).resolve()
    if not path.exists():
        return False  # Missing durable history is not proof of consumption.
    cursor = IncidentIngestCursor.objects.filter(source_identifier=str(path)).first()
    if not cursor or cursor.file_identity != _file_identity(path) or cursor.byte_offset != path.stat().st_size:
        return False
    with path.open("rb") as handle:
        if path.stat().st_size:
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                return False
    return not IncidentIngestRecord.objects.filter(status=IncidentIngestRecord.STATUS_RETRYABLE).exists()


def evidence_referenced(path):
    root = Path(settings.MEDIA_ROOT).resolve()
    absolute = Path(path).resolve()
    forms = {str(absolute), absolute.as_posix()}
    try:
        relative = absolute.relative_to(root)
        forms.update({relative.as_posix(), str(relative)})
    except ValueError:
        pass  # Supervisor logs normally live outside MEDIA_ROOT.
    # Relative canonical paths are the ingest contract; tolerate historical
    # absolute/Windows paths too, and protect regardless of evidence_valid.
    query = Q()
    for form in forms:
        query |= Q(evidence_path__iexact=form)
    return Incident.objects.filter(query).exists()


def cleanup_tick(*, dry_run=False):
    config = SupervisorConfig.from_env()
    with ExitStack() as stack:
        # Serialize against launch, including the gap before runtime takes its lease.
        stack.enter_context(SingletonLock(config.state_dir / "maintenance.lock"))
        try:
            state = read_small_json(config.state_dir / "runtime_state.json")
            current = state.get("health_snapshot_path")
            if not current or state.get("orphan_detected"):
                return {"cleanup_blocked": "runtime_ownership_unknown"}
        except (OSError, ValueError):
            return {"cleanup_blocked": "supervisor_state_unavailable"}
        safe = False
        if (float(settings.OPERATIONAL_RETENTION.get("evidence_days", 0)) > 0
                and state.get("runtime_state") == "STOPPED"
                and not _pid_exists(int(state.get("runtime_pid") or 0))):
            try:
                # Evidence deletion is an offline maintenance operation. Freeze
                # imports and appends while checking cursor/ORM references.
                stack.enter_context(SingletonLock(settings.OPERATIONAL_SERVICE_DIR / "dispatcher.lock"))
                outbox = Path(settings.INCIDENT_OUTBOX_PATH)
                stack.enter_context(SingletonLock(outbox.with_suffix(outbox.suffix + ".writer.lock")))
                safe = outbox_consumed(outbox)
            except SingletonLockError:
                safe = False
        cleaner = RetentionPass(settings.OPERATIONAL_RETENTION, referenced=evidence_referenced,
                                evidence_safe=safe, dry_run=dry_run)
        cleaner.runs(settings.PIPELINE_OUTPUT_BASE, protected=[Path(current).parent],
                     log_root=config.state_dir / "logs")
        cleaner.logs(config.state_dir / "logs", state.get("run_id"))
        result = {**cleaner.stats, "checked_at": time.time()}
        if not dry_run:
            atomic_json(config.state_dir / "cleanup_status.json", result)
        return result
