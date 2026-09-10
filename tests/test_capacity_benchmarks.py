"""Phase 16: deterministic harness guarantees; no models, decode or GPU needed."""
import csv
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from benchmarks.__main__ import parse_args
from benchmarks.control_plane import run_control
from benchmarks.real_inference import classify, prepare_config, read_json, required_cameras_complete
from benchmarks.telemetry import Output, SystemSampler, distribution, gpu_sample, redact


def test_configuration_validation():
    assert parse_args(["control_plane", "--counts", "50", "300"]).counts == [50, 300]
    for extra in (["--counts", "0"], ["--counts", "300", "300"],
                  ["--counts", "1", "--duration-sec", "nan"],
                  ["--counts", "1", "--max-samples", "0"],
                  ["--counts", "1", "--gpu-device", "0"]):
        with pytest.raises(SystemExit):
            parse_args(["control_plane", *extra])
    with pytest.raises(SystemExit):
        parse_args(["real_inference", "--counts", "1"])


def test_percentiles_and_bounded_sampler():
    assert distribution([1, 2, 3, 4, 5]) == {"samples": 5, "min": 1, "mean": 3, "p50": 3, "p95": 4.8, "max": 5}
    assert distribution([None, float("nan")])["mean"] is None
    sampler = SystemSampler(3)
    for index in range(100):
        sampler.add({"cpu_pct": index, "gpu": {"devices": []}})
    summary = sampler.summary()
    assert summary["observations"] == 100
    assert summary["retained"] == 3
    assert summary["cpu_pct"]["min"] == 97


def test_optional_gpu_and_explicit_selector():
    for error in (FileNotFoundError(), subprocess.TimeoutExpired("nvidia-smi", 2)):
        assert not gpu_sample(runner=Mock(side_effect=error))["available"]
    runner = Mock(return_value=Mock(stdout="GPU-abc, Test GPU, 80, 123, 6144\n"))
    result = gpu_sample("GPU-abc", runner=runner)
    assert result["devices"][0]["memory_total_mib"] == 6144
    assert runner.call_args.kwargs["timeout"] == 2
    assert runner.call_args.args[0][-2:] == ["--id", "GPU-abc"]


def test_output_separation_serialization_and_redaction(tmp_path):
    output = Output(tmp_path / "new", {"source": "rtsp://user:pass@private/stream?token=abc"})
    try:
        output.add({"mode": "real_inference", "run_id": "real", "password": "secret"})
        output.add({"mode": "control_plane", "run_id": "fake", "camera_equivalents": 300})
        output.sample("real", {"elapsed_sec": 1, "gpu": {"devices": []}})
        summary = json.loads((output.path / "benchmark_summary.json").read_text())
        assert [row["run_id"] for row in summary["real_inference"]] == ["real"]
        assert [row["run_id"] for row in summary["control_plane"]] == ["fake"]
        assert "private" not in json.dumps(summary) and "secret" not in json.dumps(summary)
        with (output.path / "system_samples.csv").open(newline="") as handle:
            assert json.loads(next(csv.DictReader(handle))["gpu"]) == {"devices": []}
        assert redact({"source": "C:\\private\\video.mp4"})["source"] == "[redacted-location]"
    finally:
        output.close()


def test_classification_does_not_infer_saturation_from_gpu_or_file_retries():
    def status(capacity=None, **kwargs):
        return classify(complete=kwargs.pop("complete", True), frames=100, capacity=capacity or {},
                        system={"gpu": {"a": {"utilization_pct": {"mean": 100}}}}, **kwargs)["classification"]
    assert status() == "HEALTHY"
    assert status(complete=False) == "INCOMPLETE"
    assert status(health_failed=True) == "INCOMPLETE"
    assert status({"person": {"accepted": 1, "rejected_capacity": 100, "deferred_file": 100}}) == "PRESSURED"
    assert status({"person": {"accepted": 90, "dropped_live": 10}}) == "SATURATED"
    assert status({"person": {"accepted": 98, "dropped_live": 2}}) == "PRESSURED"


def test_real_workload_identity_and_isolation(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"fixture identity only; no decode")
    base = {"models": {}, "cameras": [{"camera_id": "original", "source": str(media),
            "use_fight_detection": True, "use_speed_detection": True,
            "speed_config": {"speed_limit_kmh": 30}}],
            "runtime": {"fight_thr": .35, "incident_outbox_path": "production/outbox.jsonl"}}
    for workload in ("fight", "speed", "mixed"):
        directory = tmp_path / workload
        config, identity = prepare_config(base, repo=tmp_path, directory=directory, count=2, workload=workload)
        assert config["cameras"][0]["source"] != config["cameras"][1]["source"]
        assert all(Path(c["source"]).read_bytes() == media.read_bytes() for c in config["cameras"])
        assert all(c["use_speed_detection"] == (workload != "fight") for c in config["cameras"])
        assert config["runtime"]["fight_thr"] == base["runtime"]["fight_thr"]
        assert str(directory) in config["runtime"]["incident_outbox_path"]
        assert identity["source_type"] == "local_file"
    assert base["runtime"]["incident_outbox_path"] == "production/outbox.jsonl"
    stale = tmp_path / "partial.json"
    stale.write_text('{"partial":')
    assert read_json(stale) == {}


def test_300_control_plane_is_bounded_and_generation_safe():
    result = run_control(300, seed=17, max_samples=4, duration=.05)
    assert result["mode"] == "control_plane" and not result["real_inference"]
    assert all(result["correctness"].values())
    for stage in result["scheduler"].values():
        assert stage["round_robin_correct"]
        assert stage["outstanding"] == 0
        assert stage["dispatches"] == stage["accepted"] == 600
        assert stage["rejected_capacity"] == 600
    assert result["telemetry"]["reports_observed"] > 4
    assert result["telemetry"]["reports_retained"] <= 4
    assert result["telemetry"]["transitions_retained"] <= 4
    assert result["telemetry"]["cycles_retained"] <= 4


def test_isolated_speed_failure_is_not_a_successful_real_benchmark():
    config = {"cameras": [{"camera_id": "a", "use_speed_detection": True, "use_fight_detection": False}]}
    row = {"frames_decoded": 10, "speed_progress": 10, "fight_frames_consumed": None,
           "final_health": {"speed": {"failed": True}}}
    assert not required_cameras_complete(config, {"a": row})
    row["final_health"]["speed"]["failed"] = False
    assert required_cameras_complete(config, {"a": row})
