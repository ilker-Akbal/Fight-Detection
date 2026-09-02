import json
from pathlib import Path

from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result
from fight.pipeline.incident_outbox import IncidentOutboxEnvelope, append_envelope_durable


def _envelope(**overrides):
    values = {
        "run_id": "run-1",
        "external_incident_id": "cam-1_incident_000001",
        "camera_id": "cam-1",
        "incident_type": "FIGHT",
        "detected_at": "2026-09-02T10:00:00+00:00",
        "finalized_at": "2026-09-02T10:00:02+00:00",
        "label": "fight",
        "decision_score": 0.91,
        "max_score": 0.95,
        "mean_score": 0.88,
        "confidence": 0.91,
        "part_count": 2,
        "evidence_path": "/tmp/evidence.mp4",
    }
    values.update(overrides)
    return IncidentOutboxEnvelope.create(**values)


def test_outbox_event_id_is_unique():
    assert _envelope().event_id != _envelope().event_id


def test_outbox_preserves_run_and_external_identity():
    envelope = _envelope(run_id="run-special", external_incident_id="external-42")
    assert envelope.run_id == "run-special"
    assert envelope.external_incident_id == "external-42"


def test_durable_append_writes_one_complete_jsonl_line(tmp_path):
    target = tmp_path / "spool" / "outbox.jsonl"
    envelope = _envelope()
    append_envelope_durable(target, envelope)
    raw = target.read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw)["event_id"] == envelope.event_id


def test_outbox_schema_contains_operational_fields():
    row = _envelope().as_dict()
    required = {
        "event_id", "run_id", "external_incident_id", "camera_id",
        "incident_type", "detected_at", "finalized_at", "label",
        "decision_score", "max_score", "mean_score", "part_count",
        "evidence_path", "created_wall_time",
    }
    assert required <= set(row)


def test_finalized_incident_keeps_legacy_jsonl_and_adds_outbox(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    incidents_dir = run_dir / "incidents"
    outbox = tmp_path / "runtime_spool" / "incidents_outbox.jsonl"
    part = run_dir / "part.mp4"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"part")

    aggregator = IncidentAggregator(
        out_dir=incidents_dir,
        run_id="run-e2e",
        outbox_path=outbox,
        single_strong_fight_thr=0.5,
        clip_ready_wait_sec=0.0,
        stale_finalize_sec=60,
    )

    def fake_concat(_parts, output):
        Path(output).write_bytes(b"evidence")
        return True

    monkeypatch.setattr(aggregator, "_concat_mp4s", fake_concat)
    monkeypatch.setattr(aggregator, "_add_ai_overlay_to_clip", lambda *_: True)
    monkeypatch.setattr(aggregator, "_wait_clips_ready", lambda *_: True)
    aggregator.submit(Stage3Result(
        camera_id="cam-1",
        source="0",
        event_id="part-1",
        event_start_ts=100.0,
        event_end_ts=102.0,
        clip_path=str(part),
        fight_prob=0.95,
        fight_label="fight",
        pose_score_max=0.9,
        pose_score_mean=0.8,
    ))
    aggregator.finalize("cam-1", force=True)
    aggregator._stop_event.set()
    aggregator._sweeper.join(timeout=2)

    legacy = [json.loads(line) for line in (run_dir / "incidents.jsonl").read_text().splitlines()]
    operational = [json.loads(line) for line in outbox.read_text().splitlines()]
    assert legacy[0]["run_id"] == "run-e2e"
    assert legacy[0]["clip_path"].endswith(".mp4")
    assert operational[0]["run_id"] == "run-e2e"
    assert operational[0]["external_incident_id"] == legacy[0]["incident_id"]
