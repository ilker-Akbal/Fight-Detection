"""Conservative, bounded cleanup of *closed* runtime artifacts; ORM-free."""
from __future__ import annotations

import os
import time
from pathlib import Path

from fight.operations import run_lock_path, read_small_json
from fight.runtime_supervisor.locking import SingletonLock, SingletonLockError


def is_link(path):
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


class RetentionPass:
    def __init__(self, policy, *, referenced=lambda path: True, evidence_safe=False,
                 now=None, dry_run=False):
        self.policy = policy
        self.referenced = referenced
        self.evidence_safe = evidence_safe
        self.now = time.time() if now is None else now
        self.dry_run = dry_run
        self.max_files = max(1, int(policy.get("max_files", 500)))
        self.max_scan = max(1, int(policy.get("max_scan", 10000)))
        self.closed_run_ids = set()
        self.stats = {"scanned": 0, "removed": 0, "bytes": 0, "protected": 0,
                      "errors": 0, "scan_limited": False, "delete_limited": False,
                      "dry_run": dry_run, "evidence_enabled": evidence_safe}

    def entries(self, directory):
        # Cap enumeration as well as deletion. Never follow reparse points.
        entries = []
        try:
            with os.scandir(directory) as iterator:
                for item in iterator:
                    if self.stats["scanned"] >= self.max_scan:
                        self.stats["scan_limited"] = True
                        break
                    self.stats["scanned"] += 1
                    entries.append(Path(item.path))
        except OSError:
            self.stats["errors"] += 1
        return sorted(entries, key=lambda path: path.name)

    def old(self, path, key, default):
        days = max(0, float(self.policy.get(key, default)))
        return days > 0 and self.now - path.stat().st_mtime >= days * 86400

    def remove(self, path):
        if self.stats["removed"] >= self.max_files:
            self.stats["delete_limited"] = True
            return
        if self.referenced(path):
            self.stats["protected"] += 1
            return
        size = path.stat().st_size
        if not self.dry_run:
            path.unlink()
        self.stats["removed"] += 1
        self.stats["bytes"] += size

    def _run_files(self, directory, run):
        for path in self.entries(directory):
            if self.stats["removed"] >= self.max_files:
                self.stats["delete_limited"] = True
                break
            try:
                if is_link(path):
                    self.stats["protected"] += 1
                    continue
                if path.is_dir():
                    self._run_files(path, run)
                    if not self.dry_run:
                        try:
                            path.rmdir()  # Only empty directories; never recursive delete.
                        except OSError:
                            pass
                    continue
                name = path.name
                relative = path.relative_to(run)
                if name == ".run_state.json" or "outbox" in name or name.endswith(".lock"):
                    continue
                # Durable legacy history is deliberately retained, even after ingest.
                if name == "incidents.jsonl":
                    continue
                media = path.suffix.lower() in {".mp4", ".avi", ".mov", ".mkv"}
                if media:
                    days = float(self.policy.get("evidence_days", 0))
                    if self.evidence_safe and days > 0 and self.now - path.stat().st_mtime >= max(180, days) * 86400:
                        self.remove(path)
                    continue
                transient = (name in {"camera_status.jsonl", "events.jsonl", "stage3_results.jsonl",
                                      "events.csv", "stage3_results.csv",
                                      "performance_summary.json", "runtime_health.json"}
                             or relative.parts[0] in {"previews", "metrics", "health_history"})
                if name.endswith(".tmp") and self.old(path, "temp_days", 1):
                    self.remove(path)
                elif transient and self.old(path, "transient_days", 7):
                    self.remove(path)
                elif name.startswith("run_config") and name.endswith(".json") and self.old(path, "run_days", 30):
                    self.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                self.stats["errors"] += 1

    def runs(self, root, protected=(), log_root=None):
        root = Path(root).resolve()
        protected = {Path(path).resolve() for path in protected}
        if not root.exists():
            return
        for run in self.entries(root):
            if self.stats["removed"] >= self.max_files:
                self.stats["delete_limited"] = True
                break
            try:
                if run.name.startswith(".") or is_link(run) or not run.is_dir() or run.resolve() in protected:
                    self.stats["protected"] += 1
                    continue
                with SingletonLock(run_lock_path(run)):
                    marker = run / ".run_state.json"
                    # Unknown/aborted runs may contain pending recovery evidence.
                    state = read_small_json(marker)
                    if state.get("state") != "COMPLETED":
                        self.stats["protected"] += 1
                        continue
                    if state.get("run_id"):
                        self.closed_run_ids.add(str(state["run_id"]))
                    self._run_files(run, run)
                    logs_pending = log_root is not None and state.get("run_id") and any(
                        (Path(log_root) / f"runtime-{state['run_id']}.{stream}.log").exists()
                        for stream in ("stdout", "stderr"))
                    if self.old(marker, "run_days", 30) and not self.dry_run and not logs_pending:
                        with os.scandir(run) as items:
                            remaining = [item.name for _, item in zip(range(2), items)]
                        if remaining == [marker.name] and self.stats["removed"] < self.max_files:
                            self.remove(marker)
                            if not marker.exists():
                                run.rmdir()
            except (OSError, ValueError, SingletonLockError):
                self.stats["protected"] += 1

    def logs(self, root, current_run_id=None):
        if not Path(root).exists():
            return
        for path in self.entries(Path(root)):
            try:
                if is_link(path) or not path.is_file():
                    continue
                # Only per-run closed logs. Never touch recovery state, locks,
                # current append-only events, or active stdout/stderr handles.
                if (path.name.startswith("runtime-") and path.name.endswith((".stdout.log", ".stderr.log"))
                        and current_run_id not in {None, ""}
                        and not path.name.startswith(f"runtime-{current_run_id}.")
                        and path.name[len("runtime-"):].split(".", 1)[0] in self.closed_run_ids
                        and self.old(path, "log_days", 14)):
                    self.remove(path)
            except OSError:
                self.stats["errors"] += 1
