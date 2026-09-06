from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from fight.operations import fsync_directory
from fight.runtime_supervisor.locking import SingletonLock, SingletonLockError


class DurableWriteError(OSError):
    """Incident persistence did not complete; callers must not report success."""


OUTBOX_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class IncidentOutboxEnvelope:
    event_id: str
    run_id: str
    external_incident_id: str
    camera_id: str
    incident_type: str
    detected_at: str
    finalized_at: str
    label: str
    decision_score: float
    max_score: float
    mean_score: float
    confidence: float
    part_count: int
    evidence_path: str
    created_wall_time: float
    schema_version: int = OUTBOX_SCHEMA_VERSION
    source_system: str = "fight_runtime"

    @classmethod
    def create(cls, **values) -> "IncidentOutboxEnvelope":
        values.setdefault("event_id", str(uuid.uuid4()))
        values.setdefault("created_wall_time", time.time())
        return cls(**values)

    def as_dict(self) -> dict:
        return asdict(self)


def utc_iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def append_envelope_durable(path: str | Path, envelope: IncidentOutboxEnvelope) -> None:
    """Append one complete JSONL record and force it to stable storage."""

    target = Path(path)
    payload = (json.dumps(envelope.as_dict(), ensure_ascii=False) + "\n").encode("utf-8")

    lock = SingletonLock(target.with_suffix(target.suffix + ".writer.lock"))
    deadline = time.monotonic() + 5.0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                lock.acquire()
                break
            except SingletonLockError:
                if time.monotonic() >= deadline:
                    raise DurableWriteError("outbox_writer_busy")
                time.sleep(0.05)
        with target.open("a+b", buffering=0) as handle:
            # Preserve all bytes/offsets after a crash. Separate a partial tail
            # from the new envelope; the dispatcher records malformed lines
            # using its existing cursor and invalid-record semantics.
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    payload = b"\n" + payload
            remaining = memoryview(payload)
            while remaining:
                written = handle.write(remaining)
                if not written:
                    raise DurableWriteError("outbox_short_write")
                remaining = remaining[written:]
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(target.parent)
    except OSError as exc:
        raise DurableWriteError(exc.errno, "outbox_persistence_failed") from exc
    finally:
        lock.release()
