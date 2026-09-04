from __future__ import annotations

import inspect
import json
from pathlib import Path

import numpy as np
import pytest

from fight.pipeline import media_encoding
from fight.pipeline.incident_aggregator import IncidentAggregator


def test_h264_command_uses_browser_compatible_profile():
    command = media_encoding.build_h264_command(
        "ffmpeg",
        ["-i", "input.avi"],
        "final.mp4",
    )
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert command[-1] == "final.mp4"


@pytest.mark.skipif(
    not media_encoding.ffmpeg_path(),
    reason="ffmpeg is not installed",
)
def test_final_mp4_is_h264_avc_yuv420p(tmp_path):
    output = tmp_path / "browser.mp4"
    frames = [np.full((32, 48, 3), index * 10, dtype=np.uint8) for index in range(12)]
    assert media_encoding.save_frames_browser_mp4(frames, output, 12.0)
    codec = media_encoding.probe_video_codec(output)
    assert codec["codec_name"] == "h264"
    assert codec["codec_tag_string"] == "avc1"
    assert codec["pix_fmt"] == "yuv420p"


def test_incident_overlay_has_no_local_pose_model_or_ultralytics_import():
    source = inspect.getsource(IncidentAggregator._add_ai_overlay_to_clip)
    assert "ultralytics" not in source
    assert "INCIDENT_POSE" not in source
    assert "pose_model" not in source


def test_valid_h264_is_not_rewritten_to_opencv_fallback(tmp_path, monkeypatch):
    clip = tmp_path / "incident.mp4"
    original = b"browser-compatible-h264"
    clip.write_bytes(original)
    aggregator = IncidentAggregator.__new__(IncidentAggregator)

    monkeypatch.setattr("fight.pipeline.incident_aggregator.ffmpeg_path", lambda: None)
    monkeypatch.setattr("fight.pipeline.incident_aggregator.is_h264_video", lambda _: True)

    assert aggregator._add_ai_overlay_to_clip(clip, {}) is False
    assert clip.read_bytes() == original


def test_incident_concat_uses_h264_profile_before_fallback(tmp_path, monkeypatch):
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    output = tmp_path / "final.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    captured = {}

    def fake_encode(input_args, output_path):
        captured["args"] = list(input_args)
        Path(output_path).write_bytes(b"h264")
        return True

    monkeypatch.setattr("fight.pipeline.incident_aggregator.ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        "fight.pipeline.incident_aggregator.encode_h264_with_ffmpeg",
        fake_encode,
    )
    aggregator = IncidentAggregator.__new__(IncidentAggregator)

    assert aggregator._concat_mp4s([str(first), str(second)], output)
    assert captured["args"][:4] == ["-f", "concat", "-safe", "0"]
    assert output.read_bytes() == b"h264"


def test_jsonl_reader_is_tail_bounded_and_skips_partial_line(tmp_path):
    from Fight_backend_project.backend_frontend_project.services.pipeline_bridge import (
        report_reader,
    )

    path = tmp_path / "events.jsonl"
    complete = [json.dumps({"index": index}) for index in range(80)]
    path.write_text("\n".join(complete) + "\n" + '{"index": 999', encoding="utf-8")

    rows = report_reader.read_jsonl(path, max_rows=3, max_bytes=1024)
    assert [row["index"] for row in rows] == [77, 78, 79]


def test_jsonl_stat_cache_is_bounded(tmp_path):
    from Fight_backend_project.backend_frontend_project.services.pipeline_bridge import (
        report_reader,
    )

    report_reader._JSONL_CACHE.clear()
    for index in range(report_reader.JSONL_CACHE_MAX_ENTRIES + 5):
        path = tmp_path / f"{index}.jsonl"
        path.write_text(json.dumps({"index": index}) + "\n", encoding="utf-8")
        assert report_reader.read_jsonl(path)
    assert len(report_reader._JSONL_CACHE) == report_reader.JSONL_CACHE_MAX_ENTRIES
