from __future__ import annotations

from pathlib import Path

import pytest

from fight.pipeline.incident_aggregator import IncidentAggregator, Stage3Result


pytestmark = pytest.mark.unit


def result(event_id, start, end, prob):
    return Stage3Result(
        camera_id="cam",
        source="synthetic",
        event_id=event_id,
        event_start_ts=start,
        event_end_ts=end,
        clip_path=f"{event_id}.mp4",
        fight_prob=prob,
        fight_label="fight" if prob >= 0.5 else "non_fight",
        pose_score_max=0.8,
        pose_score_mean=0.7,
    )


@pytest.fixture
def agg(tmp_path):
    item = IncidentAggregator(
        str(tmp_path / "incidents"), clip_ready_wait_sec=0, sweep_interval_sec=0.01
    )
    yield item
    item.close_all()


def test_temporal_iou_overlap_and_normalization(agg):
    assert agg._temporal_iou(0, 10, 5, 15) == pytest.approx(1 / 3)
    assert agg._temporal_iou(0, 1, 1, 2) == 0
    normalized = agg._normalize_result(result("x", 8, 2, 1.5))
    assert (normalized.event_start_ts, normalized.event_end_ts) == (2, 8)
    assert normalized.fight_prob == 1.0


def test_duplicate_suppression_replaces_only_with_higher_probability(agg):
    st = agg._new_state("cam", "synthetic")
    agg._append_or_suppress_locked(st, result("same", 0, 8, 0.7))
    agg._append_or_suppress_locked(st, result("same", 0, 8, 0.6))
    assert st.part_count == 1 and st.max_prob == 0.7
    agg._append_or_suppress_locked(st, result("same", 0, 8, 0.9))
    assert st.part_count == 1 and st.max_prob == 0.9


def test_overlap_suppression_order_and_merge_gap(agg):
    st = agg._new_state("cam", "synthetic")
    agg._append_or_suppress_locked(st, result("later", 10, 18, 0.7))
    agg._append_or_suppress_locked(st, result("earlier", 0, 8, 0.8))
    assert [s.result.event_id for s in st.segments] == ["earlier", "later"]
    assert agg._can_merge(st, result("edge", 38, 42, 0.7))
    assert not agg._can_merge(st, result("far", 38.01, 42, 0.7))

    agg._append_or_suppress_locked(st, result("overlap", 11, 17, 0.95))
    assert st.part_count == 2
    assert any(s.result.event_id == "overlap" for s in st.segments)


def test_vote_window_thresholds_single_strong_and_nonfight(agg):
    st = agg._new_state("cam", "synthetic")
    for index, probability in enumerate((0.49, 0.52, 0.53)):
        agg._append_or_suppress_locked(
            st, result(f"e{index}", index * 10, index * 10 + 8, probability)
        )
        agg._advance_state_locked(st)
    assert st.vote_count(agg.enter_thr) == 2
    assert st.state == "confirmed"
    assert agg._final_label(st) == "fight"
    assert st.part_count == 3 and st.duration_sec == 28

    strong = agg._new_state("other", "synthetic")
    agg._append_or_suppress_locked(strong, result("strong", 0, 1, agg.single_strong_fight_thr))
    agg._advance_state_locked(strong)
    assert strong.state == "confirmed"

    weak = agg._new_state("weak", "synthetic")
    agg._append_or_suppress_locked(weak, result("weak", 0, 8, agg.keep_thr))
    agg._advance_state_locked(weak)
    assert agg._final_label(weak) == "non_fight"
