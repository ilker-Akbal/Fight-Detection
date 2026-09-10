"""Run with python -m benchmarks --help."""
from __future__ import annotations

import argparse
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from fight.runtime_supervisor.camera_state import MAX_CAMERAS
from fight.runtime_supervisor.locking import SingletonLock
from benchmarks.control_plane import run_control
from benchmarks.real_inference import Thresholds, run_real
from benchmarks.telemetry import Output, environment


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Separate real file-inference and synthetic parent/control-plane measurements.")
    parser.add_argument("mode", choices=("real_inference", "control_plane"))
    parser.add_argument("--counts", type=int, nargs="+", required=True, help="Logical cameras; existing runtime limit applies")
    parser.add_argument("--workload", choices=("fight", "speed", "mixed"), default="mixed",
                        help="mixed means both capabilities on each camera, using the Speed template's media/calibration")
    parser.add_argument("--config", type=Path, help="Real mode: existing production/effective config; models/thresholds preserved")
    parser.add_argument("--source", type=Path, help="Real mode: optional local media override (keep Speed calibration compatible)")
    parser.add_argument("--warmup-sec", type=float, default=5)
    parser.add_argument("--duration-sec", type=float, default=60, help="Measurement time after warmup; real files may finish earlier")
    parser.add_argument("--sample-interval-sec", type=float, default=1)
    parser.add_argument("--max-samples", type=int, default=256, help="Bounded tail per sampler/metric")
    parser.add_argument("--gpu-device", help="Optional NVIDIA index/UUID; sets CUDA_VISIBLE_DEVICES for child runtime and selects telemetry")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", type=Path, help="New output directory inside benchmarks/results (never overwrite)")
    for name, field in Thresholds.__dataclass_fields__.items():
        parser.add_argument("--" + name.replace("_", "-"), type=float, default=field.default)
    args = parser.parse_args(argv)
    if not args.counts or any(not 1 <= n <= MAX_CAMERAS for n in args.counts) or len(set(args.counts)) != len(args.counts):
        parser.error(f"counts must be distinct and between 1 and existing registry limit {MAX_CAMERAS}")
    if any(not math.isfinite(v) for v in (args.warmup_sec, args.duration_sec, args.sample_interval_sec)) or (
            args.warmup_sec < 0 or args.duration_sec <= 0 or args.sample_interval_sec < .1 or args.max_samples < 1):
        parser.error("finite warmup >= 0, duration > 0, sample interval >= .1 and max samples >= 1 required")
    if args.mode == "real_inference" and (not args.config or not args.config.is_file()):
        parser.error("real mode requires an existing --config")
    if args.mode == "control_plane" and (args.config or args.source or args.gpu_device):
        parser.error("config/source/device selection belong to real mode")
    args.thresholds = Thresholds(**{name: getattr(args, name) for name in Thresholds.__dataclass_fields__})
    try:
        args.thresholds.validate()
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    results = repo / "benchmarks" / "results"
    path = (args.output or results / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")).resolve()
    if path == results or results not in path.parents:
        raise ValueError("output must be a new subdirectory of benchmarks/results (git-ignored and isolated)")
    prior_device = os.environ.get("CUDA_VISIBLE_DEVICES")
    with SingletonLock(repo / "benchmarks" / ".capacity.lock"):
        output = Output(path, environment(repo, args.gpu_device))
        failed = False
        try:
            if args.gpu_device is not None:
                os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_device
            for count in args.counts:
                result = (run_real(args, count, output, repo) if args.mode == "real_inference" else
                          run_control(count, seed=args.seed, max_samples=args.max_samples, workload=args.workload,
                                      duration=args.duration_sec, warmup=args.warmup_sec))
                output.add(result)
                failed |= (result.get("classification") == "INCOMPLETE" or
                           (result["mode"] == "control_plane" and not all(result["correctness"].values())))
                print(f"{result['mode']} cameras={count}: {result.get('classification', 'complete')}", flush=True)
            print(f"Results: {path}", flush=True)
        finally:
            output.close()
            if prior_device is None:
                os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            else:
                os.environ["CUDA_VISIBLE_DEVICES"] = prior_device
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
