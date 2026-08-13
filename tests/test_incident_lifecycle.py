import json
import time
from pathlib import Path

from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result


def _aggregator(tmp_path, monkeypatch):
    agg = IncidentAggregator(
        str(tmp_path / "incidents"), stale_finalize_sec=0.01,
        cooldown_sec=60, clip_ready_wait_sec=0, sweep_interval_sec=0.01,
    )

    def concat(paths, output):
        output.write_bytes(b"".join(Path(p).read_bytes() for p in paths))
        return True

    monkeypatch.setattr(agg, "_concat_mp4s", concat)
    monkeypatch.setattr(agg, "_add_ai_overlay_to_clip", lambda *_: True)
    return agg


def _result(tmp_path, index, start, end, probability=0.9):
    clip = tmp_path / f"part-{index}.mp4"
    clip.write_bytes(f"part-{index}|".encode())
    return Stage3Result(
        camera_id="cam-1", source="synthetic", event_id=f"seg-{index}",
        event_start_ts=start, event_end_ts=end, clip_path=str(clip),
        fight_prob=probability, fight_label="fight",
        pose_score_max=0.8, pose_score_mean=0.7,
    )


def test_long_chain_waits_for_delayed_stage3_and_concatenates_all_parts(tmp_path, monkeypatch):
    """24s fight, 128 frames/part at 16fps; arrivals are notionally 12s apart."""
    agg = _aggregator(tmp_path, monkeypatch)
    try:
        agg.lifecycle("cam-1", "synthetic", "event_chain_opened")
        for index in range(3):
            agg.lifecycle("cam-1", "synthetic", "stage3_pending")
            agg.by_camera["cam-1"].last_update_wall_ts = time.time() - 12.0
            agg._sweep_once()
            assert not (tmp_path / "incidents.jsonl").exists()
            agg.submit(_result(tmp_path, index, index * 8.0, (index + 1) * 8.0))
            assert agg.by_camera["cam-1"].state != "cooldown"

        agg.lifecycle("cam-1", "synthetic", "event_chain_closed")
        row = json.loads((tmp_path / "incidents.jsonl").read_text().splitlines()[0])
        assert row["part_count"] >= 3
        assert row["duration_sec"] == 24.0
        assert Path(row["clip_path"]).read_bytes() == b"part-0|part-1|part-2|"
    finally:
        agg.close_all()


def test_stage3_drop_does_not_short_finalize_open_chain(tmp_path, monkeypatch):
    agg = _aggregator(tmp_path, monkeypatch)
    try:
        agg.lifecycle("cam-1", "synthetic", "event_chain_opened")
        agg.lifecycle("cam-1", "synthetic", "stage3_pending")
        agg.submit(_result(tmp_path, 0, 0.0, 8.0))
        agg.lifecycle("cam-1", "synthetic", "stage3_pending")
        agg.lifecycle("cam-1", "synthetic", "stage3_dropped")
        agg.by_camera["cam-1"].last_update_wall_ts = time.time() - 12.0
        agg._sweep_once()
        assert not (tmp_path / "incidents.jsonl").exists()
        assert agg.by_camera["cam-1"].state != "cooldown"
        agg.lifecycle("cam-1", "synthetic", "event_chain_closed")
        assert (tmp_path / "incidents.jsonl").exists()
    finally:
        agg.close_all()


def test_pending_success_drop_duplicate_and_closed_gate(tmp_path, monkeypatch):
    agg = _aggregator(tmp_path, monkeypatch)
    try:
        agg.lifecycle("cam-1", "synthetic", "event_chain_opened")
        agg.lifecycle("cam-1", "synthetic", "stage3_pending")
        agg.lifecycle("cam-1", "synthetic", "stage3_pending")
        assert agg.by_camera["cam-1"].pending_stage3 == 2

        first = _result(tmp_path, 0, 0.0, 8.0)
        agg.submit(first)
        assert agg.by_camera["cam-1"].pending_stage3 == 1
        agg.lifecycle("cam-1", "synthetic", "event_chain_closed")
        assert not (tmp_path / "incidents.jsonl").exists()

        agg.lifecycle("cam-1", "synthetic", "stage3_dropped")
        assert (tmp_path / "incidents.jsonl").exists()
        assert agg.by_camera["cam-1"].state == "cooldown"

        # Duplicate completion/drop cannot make the counter negative or create
        # another incident while the first one is in cooldown.
        agg.submit(first)
        agg.lifecycle("cam-1", "synthetic", "event_chain_opened")
        agg.lifecycle("cam-1", "synthetic", "stage3_dropped")
        assert agg.by_camera["cam-1"].pending_stage3 == 0
        assert len((tmp_path / "incidents.jsonl").read_text().splitlines()) == 1
    finally:
        agg.close_all()


def test_lifecycle_chain_is_never_finalized_by_stale_watchdog(tmp_path, monkeypatch):
    agg = _aggregator(tmp_path, monkeypatch)
    try:
        agg.lifecycle("cam-1", "synthetic", "event_chain_opened")
        agg.lifecycle("cam-1", "synthetic", "stage3_pending")
        agg.submit(_result(tmp_path, 0, 0.0, 8.0))
        st = agg.by_camera["cam-1"]
        st.last_update_wall_ts = time.time() - 100
        agg._sweep_once()
        assert st.state == "confirmed"
        assert not (tmp_path / "incidents.jsonl").exists()
    finally:
        agg.close_all()


def test_legacy_abandoned_state_uses_stale_watchdog(tmp_path, monkeypatch):
    agg = _aggregator(tmp_path, monkeypatch)
    try:
        agg.submit(_result(tmp_path, 0, 0.0, 8.0))
        st = agg.by_camera["cam-1"]
        assert not st.lifecycle_seen
        st.last_update_wall_ts = time.time() - 100
        agg._sweep_once()
        assert (tmp_path / "incidents.jsonl").exists()
        assert agg.by_camera["cam-1"].state == "cooldown"
    finally:
        agg.close_all()
