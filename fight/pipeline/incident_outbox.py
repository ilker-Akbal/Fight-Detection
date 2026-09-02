from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


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
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(envelope.as_dict(), ensure_ascii=False) + "\n").encode("utf-8")

    with target.open("ab", buffering=0) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
