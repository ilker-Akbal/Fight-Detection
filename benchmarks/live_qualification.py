"""Bounded LIVE qualification of the real Supervisor/runtime, not another runtime.

Run with ``python -m benchmarks.live_qualification --help``. Faults are opt-in.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import threading
import time
from collections import Counter, deque
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from benchmarks.live_source import GeneratedLiveSource
from benchmarks.real_inference import assert_no_runtime, read_json
from benchmarks.telemetry import SystemSampler, psutil, redact
from fight.operations import atomic_json
from fight.runtime_supervisor.core import RuntimeSupervisor, SupervisorConfig
from fight.runtime_supervisor.locking import SingletonLock

SCENARIOS = ("baseline", "fight-shared", "fight-local", "capability-churn", "shutdown-backoff")
IDENTITIES = ("camera_generation", "fight_consumer_epoch", "speed_epoch",
              "fight_service_epoch", "vehicle_service_epoch")
PROGRESS = ("ingest", "fight", "speed", "preview")


def source_type(source, generated=False):
    if generated:
        return GeneratedLiveSource.label
    scheme = urlsplit(str(source)).scheme.lower()
    if scheme in {"rtsp", "rtsps"}:
        return "RTSP (network interruption not injected)"
    if scheme in {"http", "https"}:
        return "LIVE HTTP (not RTSP qualification)"
    raise ValueError("use an RTSP/HTTP source or --generated; local FILE is not LIVE qualification")


def prepare_config(base, directory, camera_id=None, generated_source=None):
    config = copy.deepcopy(base)
    candidates = [c for c in config.get("cameras", []) if c.get("enabled", True)
                  and c.get("use_fight_detection", True) and c.get("use_speed_detection", False)
                  and (camera_id is None or c.get("camera_id") == camera_id)]
    if len(candidates) != 1:
        raise ValueError("select exactly one enabled mixed camera template with --camera-id")
    camera = candidates[0]
    if generated_source:
        camera["source"] = generated_source
    label = source_type(camera["source"], generated_source is not None)
    runtime = config.setdefault("runtime", {})
    if runtime.get("person_batch_enabled", False):
        raise ValueError("Phase 22 qualification requires batching OFF; config is not silently retuned")
    for key in ("run_id", "desired_camera_state_path", "health_snapshot_path"):
        runtime.pop(key, None)
    runtime.update(health_enabled=True, performance_metrics_enabled=True,
                   incident_outbox_path=str(directory / "outbox" / "incidents_outbox.jsonl"))
    config.update(cameras=[camera], run_name=directory.name, output_dir=str(directory / "runtime"))
    config.pop("run_id", None)
    if "speed" in config:
        config["speed"].update(output_dir=config["output_dir"], run_name=directory.name, cameras=[])
    return config, label


def observation(health, cid):
    camera = health.get("cameras", {}).get(cid, {})
    workers = health.get("workers", {})
    fight, speed = camera.get("fight", {}), camera.get("speed", {})
    pids = dict(camera.get("pids", {}))
    pids.update({name: row.get("pid") for name, row in workers.items()
                 if row.get("required") and row.get("service_state") in {"running", "starting"}})
    return {"camera_generation": camera.get("generation"),
            "fight_consumer_epoch": fight.get("epoch"), "speed_epoch": speed.get("epoch"),
            "fight_service_epoch": workers.get("person", {}).get("service_epoch"),
            "vehicle_service_epoch": workers.get("vehicle", {}).get("service_epoch"),
            "pids": pids, "progress": {"ingest": camera.get("ingest_progress"),
                "preview": camera.get("preview_progress"), "fight": fight.get("progress"),
                "speed": speed.get("progress")},
            "health": health.get("runtime_health"), "fight_enabled": fight.get("enabled"),
            "fight_waiting": fight.get("waiting"), "fight_failed": fight.get("failed"),
            "speed_failed": speed.get("failed"),
            "restarts": {"camera": camera.get("restart_count"), "fight_consumer": fight.get("restarts"),
                "speed_consumer": speed.get("restarts"),
                "fight_service": workers.get("person", {}).get("restart_count"),
                "vehicle": workers.get("vehicle", {}).get("restart_count"),
                "source_reconnects": camera.get("reconnect_count")},
            "capacity": {name: row.get("capacity", {}) for name, row in workers.items()},
            "written_wall_time": health.get("written_wall_time")}


def ready(row):
    return (row["health"] == "HEALTHY" and all(row[k] is not None for k in IDENTITIES)
            and all((row["progress"].get(k) or 0) > 0 for k in PROGRESS)
            and all(row["pids"].get(k) for k in ("ingest", "preview", "speed", "camera", "person", "vehicle")))


def set_fight_capability(supervisor, camera, enabled):
    current = supervisor.desired_cameras()
    return supervisor.update_desired_cameras({"schema_version": 1,
        "revision": current["revision"] + 1,
        "cameras": [{**camera, "use_fight_detection": enabled}],
        "speed_paused": current.get("speed_paused", False)})


class JsonlTail:
    """Bounded incremental read; an incomplete trailing record is retried untouched."""
    def __init__(self, path):
        self.path, self.offset, self.errors = Path(path), 0, 0
        self.caught_up = True
        self.pending_bytes = 0

    def read(self, budget=1024 * 1024):
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read(budget)
        except FileNotFoundError:
            return []
        except OSError:
            self.errors += 1
            return []
        self.caught_up = len(data) < budget
        end = data.rfind(b"\n") + 1
        self.pending_bytes = len(data) - end
        if not end and len(data) == budget:
            self.errors += 1  # Refuse an oversized record; never allocate forever.
            return []
        self.offset += end
        rows = []
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    self.errors += 1
            except (ValueError, UnicodeError):
                self.errors += 1
        return rows


class ProcessTree:
    """Bind PID + creation time to this Supervisor run before any fault action."""
    def __init__(self, pid, launch_path, limit=256):
        self.root = psutil.Process(pid)
        command = self.root.cmdline()
        if "fight.pipeline_mp.run_multiprocess" not in command or str(launch_path) not in command:
            raise RuntimeError("runtime_identity_unverified")
        self.limit, self.known = limit, {}
        self.capture()

    def capture(self):
        try:
            processes = [self.root, *self.root.children(recursive=True)] if self.root.is_running() else []
            for process in processes:
                key = (process.pid, process.create_time())
                if key not in self.known:
                    if len(self.known) >= self.limit:
                        raise RuntimeError("process_observation_limit_exceeded")
                    self.known[key] = process
        except psutil.NoSuchProcess:
            pass

    def alive(self):
        return [p for p in self.known.values() if p.is_running()]

    def verified_child(self, pid, snapshot_time):
        self.capture()
        if sum(number == pid for number, _ in self.known) != 1:
            raise RuntimeError("fault_target_pid_reused_or_unknown")
        matches = [p for (number, created), p in self.known.items()
                   if number == pid and created <= snapshot_time and p.is_running()]
        if len(matches) != 1 or pid == self.root.pid:
            raise RuntimeError("fault_target_ambiguous_or_stale")
        process = matches[0]
        # Require the current direct runtime child, not an unrelated reused PID.
        if not self.root.is_running() or process.ppid() != self.root.pid:
            raise RuntimeError("fault_target_not_current_runtime_child")
        return process

    def inject(self, row, role):
        stamp = row.get("written_wall_time")
        if stamp is None or not 0 <= time.time() - stamp <= 10:
            raise RuntimeError("fault_snapshot_not_fresh")
        process = self.verified_child(row["pids"].get(role), stamp)
        identity = {"role": role, "pid": process.pid, "created": process.create_time()}
        process.kill()  # Explicit scenario only; normal command never reaches here.
        return identity


class Qualification:
    """Diagnostic expectations over real observations; never controls runtime recovery."""
    def __init__(self, scenario, label, camera_id, max_samples=256):
        self.scenario, self.label, self.cid = scenario, label, camera_id
        self.start = self.end = None
        self.samples = deque(maxlen=max_samples)
        self.changes, self.events = deque(maxlen=64), deque(maxlen=64)
        self.failures, self.counts = set(), Counter()
        self.action = None
        self.disabled = self.pending = False
        self.source_pids = set()
        self.speed_during_recovery = None
        self.rss_start = self.rss_end = self.rss_peak = None
        self.rss_count = 0

    def consume_events(self, rows, run_id):
        for row in rows:
            if row.get("run_id") != run_id:
                continue
            detail = str(row.get("detail", ""))
            self.counts["status_rows_observed"] += 1
            ingest = row.get("pids", {}).get("ingest")
            if ingest and len(self.source_pids) < 256:
                self.source_pids.add(ingest)
            if detail == "stopped" and row.get("stage") in {"orchestrator", "reporter"}:
                self.counts[row["stage"] + "_stopped"] += 1
            if "stale" in detail or "rejected" in detail:
                self.counts["stale_or_rejected_status_events"] += 1
            if detail == "health_snapshot_write_failed":
                self.counts[detail] += 1
            if any(word in detail for word in ("restarting", "restart_failed", "service_failed", "reconnecting")):
                self.counts["recovery_status_events"] += 1
                self.events.append({key: redact(row.get(key)) for key in
                    ("ts", "detail", "component", "reason", "component_failure", "service_epoch", "retries", "exit_code")})
            if detail == "fight_service_restarting":
                self.counts["fight_service_restarting"] += 1
                if self.action:
                    self.pending = True

    def add(self, row, elapsed, system=None):
        self.counts["observations"] += 1
        self.counts["health_failed_samples"] += int(row["health"] == "FAILED")
        if system and system.get("runtime_tree_rss_bytes") is not None:
            rss = system["runtime_tree_rss_bytes"]
            if self.rss_start is None:
                self.rss_start = rss
            self.rss_end, self.rss_peak = rss, max(self.rss_peak or rss, rss)
            self.rss_count += 1
        if self.start is None:
            if not ready(row):
                return
            self.start = copy.deepcopy(row)
        previous = self.end or self.start
        self.end = copy.deepcopy(row)
        self.samples.append({"elapsed_sec": elapsed, **row})
        if row["health"] == "FAILED" or row["speed_failed"]:
            self.failures.add("runtime_or_speed_failed")
        if row["fight_waiting"] and self.action:
            self.pending = True
            progress = row["progress"].get("speed")
            if progress is not None:
                self.speed_during_recovery = max(self.speed_during_recovery or progress, progress)
        if self.scenario == "capability-churn" and self.action and not row["fight_enabled"]:
            self.disabled = True
        for key in IDENTITIES:
            initial, current = self.start[key], row[key]
            if current is None:
                continue
            if current != previous.get(key):
                self.changes.append({"elapsed_sec": elapsed, "identity": key,
                                     "before": previous.get(key), "after": current})
            delta = current - initial
            allowed = 0
            if self.action and key == "fight_consumer_epoch" and self.scenario != "shutdown-backoff":
                allowed = 1
            if self.action and key == "fight_service_epoch" and self.scenario in {"fight-shared", "capability-churn"}:
                allowed = 1
            if not 0 <= delta <= allowed:
                self.failures.add("unexpected_" + key)
        for role, pid in row["pids"].items():
            before = self.start["pids"].get(role)
            last = previous["pids"].get(role)
            if pid != last:
                self.changes.append({"elapsed_sec": elapsed, "identity": "pid_" + role,
                                     "before": last, "after": pid})
            replaceable = (self.action and (role == "camera" or
                (role in {"person", "person_router", "pose", "pose_router", "stage3"}
                 and self.scenario in {"fight-shared", "capability-churn"})))
            if before and pid and before != pid and not replaceable:
                self.failures.add("unexpected_process_change_" + role)
        for name, count in row["restarts"].items():
            if count is None:
                continue
            allowed = 1 if self.action and name in {"fight_consumer", "fight_service"} else 0
            if count > allowed:
                self.failures.add("restart_storm_or_unexpected_" + name)

    def finish(self, *, duration, completed, exit_code, leaks, system, error=None,
               observation_errors=0, source_connections=None):
        incomplete = []
        if error:
            incomplete.append(error)
        if not self.start or not self.end:
            incomplete.append("required_progress_or_identity_unavailable")
        if not completed:
            incomplete.append("scenario_deadline_not_completed")
        if observation_errors:
            incomplete.append("observation_gap")
        if exit_code is None:
            incomplete.append("runtime_exit_unavailable")
        elif exit_code != 0:
            self.failures.add("nonzero_runtime_exit")
        if leaks:
            self.failures.add("child_process_leak")
        if source_connections and source_connections["peak"] > 1:
            self.failures.add("overlapping_generated_source_connections")
        if self.start and self.end:
            if self.scenario != "shutdown-backoff":
                if not ready(self.end):
                    self.failures.add("required_branch_not_healthy_at_end")
                for name in PROGRESS:
                    a, b = self.start["progress"].get(name), self.end["progress"].get(name)
                    if a is None or b is None:
                        incomplete.append(name + "_progress_unavailable")
                    elif name == "fight" and self.action:
                        if b <= 0:
                            self.failures.add("fight_never_resumed")
                    elif b <= a:
                        self.failures.add(name + "_progress_stopped")
            if self.scenario in {"fight-shared", "fight-local", "capability-churn"}:
                if not self.action or self.end["fight_consumer_epoch"] != self.start["fight_consumer_epoch"] + 1:
                    self.failures.add("expected_fight_replacement_missing")
                if self.end["pids"].get("camera") == self.start["pids"].get("camera"):
                    self.failures.add("expected_fight_process_replacement_missing")
            if self.scenario == "fight-shared" and self.end["fight_service_epoch"] != self.start["fight_service_epoch"] + 1:
                self.failures.add("expected_shared_recovery_missing")
            if self.scenario in {"fight-shared", "shutdown-backoff"} and self.action:
                before = self.action.get("speed_progress_before", self.start["progress"]["speed"])
                if self.speed_during_recovery is None or self.speed_during_recovery <= before:
                    incomplete.append("continuing_speed_progress_during_backoff_not_observed")
            if self.scenario == "capability-churn" and not self.disabled:
                incomplete.append("disabled_phase_not_observed")
            if self.scenario == "shutdown-backoff" and not (self.action and self.pending):
                incomplete.append("recovery_backoff_not_observed_before_stop")
        result = "FAIL" if self.failures else "INCOMPLETE" if incomplete else (
            "PASS" if self.scenario == "baseline" else "PASS_WITH_RECOVERY")
        return {"schema_version": 1, "classification": result, "scenario": self.scenario,
            "duration_sec": duration, "camera_id": self.cid, "source_type": self.label,
            "failures": sorted(self.failures), "incomplete_reasons": incomplete,
            "identity_start_end": {key: {"start": self.start[key] if self.start else None,
                "end": self.end[key] if self.end else None} for key in IDENTITIES},
            "progress_start_end": {"start": self.start["progress"] if self.start else None,
                                   "end": self.end["progress"] if self.end else None},
            "processes_start_end": {"start": self.start["pids"] if self.start else None,
                                     "end": self.end["pids"] if self.end else None},
            "identity_changes": list(self.changes), "recovery_events": list(self.events),
            "restart_counts": self.end["restarts"] if self.end else None,
            "capacity_counters": self.end["capacity"] if self.end else None,
            "counts": dict(self.counts), "injected_action": self.action,
            "speed_progress_observed_during_recovery": self.speed_during_recovery,
            "health_failed_samples": self.counts["health_failed_samples"],
            "snapshot_write_failure_events": self.counts["health_snapshot_write_failed"],
            "runtime_exit_code": exit_code, "unexpected_children_after_shutdown": leaks,
            "rss_bytes": {"start": self.rss_start, "end": self.rss_end, "peak": self.rss_peak,
                "samples": self.rss_count, "delta": self.rss_end - self.rss_start if self.rss_count else None},
            "system": system, "gpu_samples": system.get("gpu") or None,
            "generated_source_connections": source_connections,
            "observation_errors": observation_errors,
            "observed_source_pids": sorted(self.source_pids),
            "stale_durable_publication_verified": None,
            "stale_durable_verification_reason": "outbox lacks publication-floor history; absence of incidents is not fencing proof",
            "observation_scope": "sampled identities/progress; no proof against sub-sample transients; bounded event/sample tails"}


def run(args):
    repo = Path(__file__).resolve().parents[1]
    assert_no_runtime()
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, exist_ok=False)  # Never overwrite historical runs.
    with ExitStack() as stack:
        stack.enter_context(SingletonLock(repo / "benchmarks" / ".capacity.lock"))
        fixture = GeneratedLiveSource() if args.generated else None
        source = stack.enter_context(fixture) if fixture else None
        config, label = prepare_config(read_json(args.config), directory, args.camera_id, source)
        camera = config["cameras"][0]
        config_path = directory / "qualification_config.json"
        atomic_json(config_path, config)
        config_path.chmod(0o600)
        supervisor = RuntimeSupervisor(SupervisorConfig(repo_root=repo, state_dir=directory / "supervisor",
            allowed_config_dirs=(directory,), auto_restart=False))
        stack.callback(supervisor.close, stop_runtime=False)
        qualifier = Qualification(args.scenario, label, camera["camera_id"], args.max_samples)
        sampler = SystemSampler(args.max_samples, args.gpu_device)
        statuses = JsonlTail(directory / "runtime" / "camera_status.jsonl")
        outbox = JsonlTail(directory / "outbox" / "incidents_outbox.jsonl")
        start = time.monotonic()
        measured_start = None
        tree = None
        completed = False
        error = None
        gaps = envelopes = 0
        started = {}
        restored = False
        stop_wall = None
        measured_duration = 0
        try:
            started = supervisor.start(config_path)
            tree = ProcessTree(started["runtime_pid"], started["launch_config_path"])
            print(f"qualification {label}; scenario={args.scenario}; run_id={started['run_id']}", flush=True)
            while True:
                status = supervisor.status()
                tree.capture()
                qualifier.consume_events(statuses.read(), started["run_id"])
                envelopes += len(outbox.read())
                if status.get("runtime_exit_code") is not None:
                    qualifier.failures.add("unexpected_runtime_exit")
                    break
                elapsed = time.monotonic() - start
                health = read_json(directory / "runtime" / "runtime_health.json")
                valid = (health.get("run_id") == started["run_id"] and
                         0 <= time.time() - health.get("written_wall_time", 0) <= 10)
                if valid:
                    row = observation(health, camera["camera_id"])
                    system = sampler.sample(elapsed, started["runtime_pid"])
                    qualifier.add(row, elapsed, system)
                    if qualifier.start and measured_start is None:
                        measured_start = time.monotonic()
                    # Verify preserved owners still belong to this tree. Old
                    # role PIDs are retained for overlap/leak checks until exit.
                    if qualifier.start:
                        alive_pids = {p.pid for p in tree.alive()}
                        if len(qualifier.source_pids & alive_pids) > 1:
                            qualifier.failures.add("duplicate_observed_source_owner")
                        for role in ("ingest", "preview", "speed", "vehicle"):
                            pid = qualifier.start["pids"][role]
                            if pid not in alive_pids:
                                qualifier.failures.add("preserved_owner_died_" + role)
                elif measured_start is not None:
                    gaps += 1
                age = time.monotonic() - measured_start if measured_start is not None else 0
                if valid and measured_start is not None and age >= args.duration_sec / 4 and not qualifier.action:
                    if args.scenario in {"fight-shared", "fight-local", "shutdown-backoff"}:
                        role = "camera" if args.scenario == "fight-local" else "person"
                        qualifier.action = {**tree.inject(row, role), "elapsed_sec": elapsed,
                                            "speed_progress_before": row["progress"]["speed"]}
                    elif args.scenario == "capability-churn":
                        set_fight_capability(supervisor, camera, False)
                        qualifier.action = {"action": "disable_fight", "elapsed_sec": elapsed}
                if args.scenario == "capability-churn" and qualifier.disabled and not restored and age >= args.duration_sec / 2:
                    set_fight_capability(supervisor, camera, True)
                    restored = True
                if args.scenario == "shutdown-backoff" and qualifier.pending:
                    completed = True
                    break
                if measured_start is not None and age >= args.duration_sec:
                    completed = args.scenario != "shutdown-backoff"
                    break
                if measured_start is None and elapsed >= args.startup_timeout_sec:
                    error = "startup_readiness_deadline"
                    break
                time.sleep(args.sample_interval_sec)
        except (OSError, ValueError, RuntimeError, psutil.Error) as exc:
            error = type(exc).__name__  # Do not serialize exceptions containing source credentials.
        except KeyboardInterrupt:
            error = "operator_interrupted"
        finally:
            measured_duration = time.monotonic() - measured_start if measured_start else 0
            stop_wall = time.time()
            stop_errors = []
            def stop():
                try:
                    supervisor.stop()
                except (OSError, RuntimeError) as exc:
                    stop_errors.append(type(exc).__name__)
            stopper = threading.Thread(target=stop, daemon=True)
            stopper.start()
            deadline = time.monotonic() + 20
            while stopper.is_alive() and time.monotonic() < deadline:
                if tree:
                    try:
                        tree.capture()
                    except (RuntimeError, psutil.Error):
                        gaps += 1
                stopper.join(.2)
            if stopper.is_alive() or stop_errors:
                error = "shutdown_deadline_or_error"
        leaks = []
        if tree:
            settle_deadline = time.monotonic() + 3
            while tree.alive() and time.monotonic() < settle_deadline:
                time.sleep(.1)
            for process in tree.alive():
                leaks.append({"pid": process.pid, "created": process.create_time()})
            if any(created > stop_wall for (_, created) in tree.known):
                qualifier.failures.add("child_spawned_after_stop_requested")
        # Reporter may only flush its final rows during shutdown. Bounded catch-up.
        for _ in range(8):
            qualifier.consume_events(statuses.read(), started.get("run_id"))
            envelopes += len(outbox.read())
            if statuses.caught_up and outbox.caught_up:
                break
        gaps += statuses.errors + outbox.errors + int(not statuses.caught_up or not outbox.caught_up
                                                      or statuses.pending_bytes or outbox.pending_bytes)
        if not qualifier.counts["orchestrator_stopped"] or not qualifier.counts["reporter_stopped"]:
            gaps += 1  # Do not call an abruptly truncated shutdown fully qualified.
        final_state = read_json(supervisor.state_path)
        result = qualifier.finish(duration=time.monotonic() - start, completed=completed,
            exit_code=final_state.get("runtime_exit_code"), leaks=leaks, system=sampler.summary(),
            error=error, observation_errors=gaps,
            source_connections={"total": fixture.connections, "peak": fixture.peak} if fixture else None)
        result.update(run_id=started.get("run_id"), requested_soak_sec=args.duration_sec,
                      measured_soak_sec=measured_duration,
                      runtime_restart_count=final_state.get("restart_count"),
                      observed_process_identities=[{"pid": pid, "created": created}
                          for pid, created in tree.known] if tree else None,
                      outbox_envelopes_observed=envelopes)
        atomic_json(directory / "qualification_summary.json", redact(result))
        print(f"{result['classification']}: {directory / 'qualification_summary.json'}", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="existing mixed-camera runtime JSON config")
    parser.add_argument("--camera-id", help="mixed camera template to qualify")
    parser.add_argument("--output", required=True, type=Path, help="NEW private directory (never reused)")
    parser.add_argument("--scenario", choices=SCENARIOS, default="baseline", help="faults are opt-in")
    parser.add_argument("--generated", action="store_true", help="generated local HTTP MJPEG, NOT real RTSP proof")
    parser.add_argument("--duration-sec", type=float, default=300, help="soak AFTER first healthy progress")
    parser.add_argument("--startup-timeout-sec", type=float, default=120)
    parser.add_argument("--sample-interval-sec", type=float, default=1)
    parser.add_argument("--max-samples", type=int, default=256)
    parser.add_argument("--gpu-device")
    args = parser.parse_args()
    if (not all(math.isfinite(x) and x > 0 for x in
                (args.duration_sec, args.startup_timeout_sec, args.sample_interval_sec))
            or args.duration_sec < 5 or args.sample_interval_sec < .25 or not 2 <= args.max_samples <= 4096):
        parser.error("finite positive durations, duration >= 5, interval >= .25, samples 2..4096 required")
    try:
        result = run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"qualification refused: {type(exc).__name__}\n")
    return 0 if result["classification"].startswith("PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
