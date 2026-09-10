"""Bounded, optional host telemetry and credential-free benchmark output."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import platform
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fight.operations import atomic_json
from fight.pipeline_mp.performance import percentile

try:
    import psutil
except ImportError:
    psutil = None


def distribution(values):
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not values:
        return {"samples": 0, "min": None, "mean": None, "p50": None, "p95": None, "max": None}
    return {"samples": len(values), "min": min(values), "mean": sum(values) / len(values),
            "p50": percentile(values, .5), "p95": percentile(values, .95), "max": max(values)}


def redact(value):
    """Do not publish even a credential-stripped private endpoint or local path."""
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if any(s in str(k).lower() for s in
                ("password", "secret", "token", "authorization", "credential")) else redact(v))
                for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(v) for v in value]
    if isinstance(value, str) and ("://" in value or value.startswith(("/", "\\\\"))
                                  or (len(value) > 2 and value[1:3] in (":\\", ":/"))):
        return "[redacted-location]"
    return value


def gpu_sample(device=None, *, runner=subprocess.run):
    command = ["nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total",
               "--format=csv,noheader,nounits"]
    if device is not None:
        command += ["--id", str(device)]
    try:
        result = runner(command, capture_output=True, text=True, timeout=2, check=True)
        rows = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) != 5:
                continue
            def number(text):
                try:
                    value = float(text.strip())
                    return value if math.isfinite(value) else None
                except ValueError:
                    return None
            rows.append(dict(zip(("uuid", "name", "utilization_pct", "memory_used_mib", "memory_total_mib"),
                                 [row[0].strip(), row[1].strip(), *map(number, row[2:])])) )
        return {"available": bool(rows), "devices": rows, "reason": None if rows else "no_device_data"}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"available": False, "devices": [], "reason": "nvidia_telemetry_unavailable"}


def rss_bytes():
    return psutil.Process().memory_info().rss if psutil else None


class SystemSampler:
    def __init__(self, max_samples=256, gpu_device=None):
        if max_samples < 1:
            raise ValueError("max_samples must be positive")
        self.samples = deque(maxlen=max_samples)
        self.observations = 0
        self.gpu_device = gpu_device
        if psutil:
            psutil.cpu_percent(None)  # Prime the host CPU delta; no blocking interval.

    def sample(self, elapsed, runtime_pid=None):
        row = {"elapsed_sec": elapsed, "cpu_pct": None, "runtime_tree_rss_bytes": None,
               "harness_rss_bytes": rss_bytes(), "ram_used_bytes": None, "ram_total_bytes": None,
               "gpu": gpu_sample(self.gpu_device)}
        if psutil:
            memory = psutil.virtual_memory()
            row.update(cpu_pct=psutil.cpu_percent(None), ram_used_bytes=memory.used, ram_total_bytes=memory.total)
            if runtime_pid:
                try:
                    parent = psutil.Process(runtime_pid)
                    total = 0
                    for process in [parent, *parent.children(recursive=True)]:
                        try:
                            total += process.memory_info().rss
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    row["runtime_tree_rss_bytes"] = total
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        self.add(row)
        return row

    def add(self, row):
        self.observations += 1
        self.samples.append(row)

    def summary(self):
        result = {"observations": self.observations, "retained": len(self.samples),
                  "capacity": self.samples.maxlen, "scope": "most recent bounded measurement samples",
                  "host_metrics_available": psutil is not None}
        for key in ("cpu_pct", "runtime_tree_rss_bytes", "harness_rss_bytes", "ram_used_bytes", "ram_total_bytes"):
            result[key] = distribution(row.get(key) for row in self.samples)
        devices = {}
        for sample in self.samples:
            for gpu in sample.get("gpu", {}).get("devices", []):
                devices.setdefault(gpu["uuid"], []).append(gpu)
        result["gpu"] = {key: {"name": rows[-1]["name"], **{metric: distribution(r[metric] for r in rows)
                            for metric in ("utilization_pct", "memory_used_mib", "memory_total_mib")}}
                         for key, rows in devices.items()}
        return result


def environment(repo, gpu_device=None):
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                             text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = None
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "os": platform.platform(),
            "python": platform.python_version(), "cpu_logical_count": os.cpu_count(),
            "ram_total_bytes": psutil.virtual_memory().total if psutil else None,
            "gpu": gpu_sample(gpu_device), "repo_sha": sha}


def file_identity(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"source_type": "local_file", "sha256": digest.hexdigest(),
            "bytes": Path(path).stat().st_size, "extension": Path(path).suffix.lower()}


class Output:
    FIELDS = ("run_id", "elapsed_sec", "cpu_pct", "runtime_tree_rss_bytes", "harness_rss_bytes",
              "ram_used_bytes", "ram_total_bytes", "gpu")

    def __init__(self, path, metadata):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.summary = {"schema_version": 1, "environment": metadata, "real_inference": [], "control_plane": []}
        self.csv_file = (self.path / "system_samples.csv").open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.csv_file, fieldnames=self.FIELDS)
        self.writer.writeheader()
        atomic_json(self.path / "benchmark_summary.json", redact(self.summary))

    def sample(self, run_id, sample):
        row = {"run_id": run_id, **sample, "gpu": json.dumps(redact(sample["gpu"]), allow_nan=False)}
        self.writer.writerow(row)
        self.csv_file.flush()

    def add(self, result):
        mode = result["mode"]
        if mode not in ("real_inference", "control_plane"):
            raise ValueError("invalid benchmark mode")
        safe = redact(result)
        with (self.path / "runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.summary[mode].append(safe)
        atomic_json(self.path / "benchmark_summary.json", redact(self.summary))

    def close(self):
        self.csv_file.close()
