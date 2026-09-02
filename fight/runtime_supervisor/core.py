from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


STOPPED = "STOPPED"
STARTING = "STARTING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
FAILED = "FAILED"
BACKOFF = "BACKOFF"
ACTIVE_STATES = {STARTING, RUNNING, STOPPING, BACKOFF}


class InvalidRuntimeConfig(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _pid_exists(pid: int) -> bool:
    if int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class SupervisorConfig:
    repo_root: Path
    state_dir: Path
    allowed_config_dirs: tuple[Path, ...]
    stop_grace_sec: float = 8.0
    kill_grace_sec: float = 3.0
    auto_restart: bool = False
    restart_max_attempts: int = 2
    restart_window_sec: float = 300.0
    restart_backoff_sec: float = 3.0
    monitor_interval_sec: float = 0.25

    @classmethod
    def from_env(cls) -> "SupervisorConfig":
        repo_root = Path(
            os.getenv("RUNTIME_SUPERVISOR_REPO_ROOT", str(_default_repo_root()))
        ).resolve()
        state_dir = Path(
            os.getenv(
                "RUNTIME_SUPERVISOR_STATE_DIR",
                str(repo_root / ".runtime_supervisor"),
            )
        ).resolve()
        raw_allowed = os.getenv("RUNTIME_SUPERVISOR_ALLOWED_CONFIG_DIRS", "").strip()
        allowed = (
            tuple(Path(item).resolve() for item in raw_allowed.split(os.pathsep) if item)
            if raw_allowed
            else (repo_root,)
        )
        return cls(
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_config_dirs=allowed,
            stop_grace_sec=float(os.getenv("SUPERVISOR_STOP_GRACE_SEC", "8")),
            kill_grace_sec=float(os.getenv("SUPERVISOR_KILL_GRACE_SEC", "3")),
            auto_restart=os.getenv("SUPERVISOR_AUTO_RESTART", "false").lower()
            in {"1", "true", "yes", "on"},
            restart_max_attempts=max(
                0, int(os.getenv("SUPERVISOR_RESTART_MAX_ATTEMPTS", "2"))
            ),
            restart_window_sec=max(
                1.0, float(os.getenv("SUPERVISOR_RESTART_WINDOW_SEC", "300"))
            ),
            restart_backoff_sec=max(
                0.0, float(os.getenv("SUPERVISOR_RESTART_BACKOFF_SEC", "3"))
            ),
        )


class RuntimeSupervisor:
    """Thread-safe owner of exactly one run_multiprocess parent process."""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        popen_factory: Callable = subprocess.Popen,
        command_runner: Callable = subprocess.run,
        pid_exists: Callable[[int], bool] = _pid_exists,
        platform_name: str | None = None,
        start_monitor: bool = True,
    ):
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._popen_factory = popen_factory
        self._command_runner = command_runner
        self._pid_exists = pid_exists
        self._platform_name = platform_name or os.name
        self._lock = threading.RLock()
        self._child = None
        self._stdout_handle = None
        self._stderr_handle = None
        self._monitor_stop = threading.Event()
        self._restart_attempts: deque[float] = deque()
        self._state = self._fresh_state()
        self._reconcile_persisted_state()
        self._record("supervisor_started")
        self._monitor_thread = None
        if start_monitor:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="runtime-supervisor-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    @property
    def state_path(self) -> Path:
        return self.config.state_dir / "runtime_state.json"

    @property
    def telemetry_path(self) -> Path:
        return self.config.state_dir / "supervisor_events.jsonl"

    def _fresh_state(self) -> dict:
        return {
            "supervisor_state": STOPPED,
            "supervisor_pid": os.getpid(),
            "runtime_pid": None,
            "runtime_state": STOPPED,
            "runtime_started_at": None,
            "runtime_exit_code": None,
            "last_exit_at": None,
            "config_path": None,
            "launch_config_path": None,
            "run_id": None,
            "restart_count": 0,
            "last_failure": None,
            "orphan_detected": False,
        }

    def _persist(self) -> None:
        payload = dict(self._state)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _record(self, detail: str, **extra) -> None:
        row = {
            "ts": _utc_now(),
            "detail": str(detail),
            "supervisor_pid": os.getpid(),
            "runtime_pid": self._state.get("runtime_pid"),
            "run_id": self._state.get("run_id"),
        }
        row.update(extra)
        with open(self.telemetry_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _reconcile_persisted_state(self) -> None:
        if not self.state_path.exists():
            self._persist()
            return
        try:
            previous = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            self._state["runtime_state"] = FAILED
            self._state["supervisor_state"] = FAILED
            self._state["last_failure"] = "persisted_state_invalid"
            self._persist()
            return

        previous_pid = previous.get("runtime_pid")
        previous_state = str(previous.get("runtime_state") or STOPPED)
        self._state.update(
            {
                "runtime_exit_code": previous.get("runtime_exit_code"),
                "last_exit_at": previous.get("last_exit_at"),
                "config_path": previous.get("config_path"),
                "run_id": previous.get("run_id"),
                "restart_count": int(previous.get("restart_count", 0) or 0),
            }
        )
        if previous_pid and previous_state in ACTIVE_STATES | {RUNNING}:
            if self._pid_exists(int(previous_pid)):
                self._state.update(
                    {
                        "supervisor_state": FAILED,
                        "runtime_state": FAILED,
                        "runtime_pid": int(previous_pid),
                        "last_failure": "orphan_detected_unverified_process",
                        "orphan_detected": True,
                    }
                )
                self._persist()
                self._record("orphan_detected", persisted_pid=int(previous_pid))
                return
            self._record("stale_pid_reconciled", persisted_pid=int(previous_pid))
        self._state.update(
            {
                "supervisor_state": STOPPED,
                "runtime_state": STOPPED,
                "runtime_pid": None,
                "orphan_detected": False,
            }
        )
        self._persist()

    def _validate_config_path(self, config_path: str | Path) -> tuple[Path, dict]:
        candidate = Path(config_path).expanduser().resolve()
        if candidate.suffix.lower() != ".json" or not candidate.is_file():
            raise InvalidRuntimeConfig("runtime config must be an existing JSON file")
        if not any(_path_is_within(candidate, root) for root in self.config.allowed_config_dirs):
            raise InvalidRuntimeConfig("runtime config path is outside allowed directories")
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            raise InvalidRuntimeConfig("runtime config is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise InvalidRuntimeConfig("runtime config root must be an object")
        if not isinstance(payload.get("cameras"), list) or not payload["cameras"]:
            raise InvalidRuntimeConfig("runtime config must contain at least one camera")
        output_value = str(payload.get("output_dir") or "").strip()
        if not output_value:
            raise InvalidRuntimeConfig("runtime config output_dir is required")
        output_path = Path(output_value)
        if not output_path.is_absolute():
            output_path = self.config.repo_root / output_path
        output_path = output_path.resolve()
        if not _path_is_within(output_path, self.config.repo_root):
            raise InvalidRuntimeConfig("runtime output_dir must stay inside repository root")
        for camera in payload["cameras"]:
            if not isinstance(camera, dict):
                raise InvalidRuntimeConfig("each camera entry must be an object")
            if not str(camera.get("camera_id") or "").strip():
                raise InvalidRuntimeConfig("each camera requires camera_id")
            if not str(camera.get("source") or "").strip():
                raise InvalidRuntimeConfig("each camera requires source")
        return candidate, payload

    def _make_launch_config(self, requested_path: Path, payload: dict, run_id: str) -> Path:
        launch_payload = dict(payload)
        launch_payload["run_id"] = run_id
        runtime = dict(launch_payload.get("runtime") or {})
        runtime["run_id"] = run_id
        launch_payload["runtime"] = runtime
        launch_path = requested_path.with_name(
            f"{requested_path.stem}.supervisor-{run_id}{requested_path.suffix}"
        )
        temporary = launch_path.with_suffix(launch_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(launch_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(launch_path)
        try:
            launch_path.chmod(0o600)
        except OSError:
            pass
        return launch_path

    def _build_command(self, launch_path: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "fight.pipeline_mp.run_multiprocess",
            "--config",
            str(launch_path),
        ]

    def _close_log_handles(self) -> None:
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _launch_locked(self, config_path: str | Path, *, is_restart: bool) -> dict:
        requested_path, payload = self._validate_config_path(config_path)
        run_id = uuid.uuid4().hex
        launch_path = self._make_launch_config(requested_path, payload, run_id)
        logs_dir = self.config.state_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = logs_dir / f"runtime-{run_id}.stdout.log"
        stderr_path = logs_dir / f"runtime-{run_id}.stderr.log"
        self._stdout_handle = open(stdout_path, "a", encoding="utf-8", buffering=1)
        self._stderr_handle = open(stderr_path, "a", encoding="utf-8", buffering=1)
        self._state.update(
            {
                "supervisor_state": STARTING,
                "runtime_state": STARTING,
                "runtime_pid": None,
                "runtime_started_at": None,
                "runtime_exit_code": None,
                "last_exit_at": None,
                "config_path": str(requested_path),
                "launch_config_path": str(launch_path),
                "run_id": run_id,
                "last_failure": None,
                "orphan_detected": False,
            }
        )
        if is_restart:
            self._state["restart_count"] = int(self._state.get("restart_count", 0)) + 1
        self._persist()
        self._record("runtime_start_requested")

        env = os.environ.copy()
        repo_root = str(self.config.repo_root)
        current_pythonpath = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = (
            repo_root + os.pathsep + current_pythonpath if current_pythonpath else repo_root
        )
        env.setdefault("PYTHONUNBUFFERED", "1")
        kwargs = {
            "cwd": repo_root,
            "env": env,
            "stdout": self._stdout_handle,
            "stderr": self._stderr_handle,
            "text": True,
        }
        if self._platform_name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            child = self._popen_factory(self._build_command(launch_path), **kwargs)
        except Exception as exc:
            self._close_log_handles()
            self._state.update(
                {
                    "supervisor_state": FAILED,
                    "runtime_state": FAILED,
                    "last_failure": f"runtime_spawn_failed: {type(exc).__name__}",
                }
            )
            self._persist()
            self._record("runtime_failed", failure_reason="runtime_spawn_failed")
            raise

        self._child = child
        started_at = _utc_now()
        self._state.update(
            {
                "supervisor_state": RUNNING,
                "runtime_state": RUNNING,
                "runtime_pid": int(child.pid),
                "runtime_started_at": started_at,
            }
        )
        self._persist()
        self._record("runtime_started")
        response = self.status()
        response["result"] = "started"
        return response

    def start(self, config_path: str | Path) -> dict:
        with self._lock:
            self._refresh_child_locked()
            if self._child is not None and self._state["runtime_state"] in {
                STARTING,
                RUNNING,
                STOPPING,
            }:
                response = self.status()
                response["result"] = "already_running"
                return response
            return self._launch_locked(config_path, is_restart=False)

    def _wait_child(self, child, timeout: float) -> bool:
        try:
            child.wait(timeout=max(0.0, float(timeout)))
            return True
        except (subprocess.TimeoutExpired, TimeoutError):
            return False

    def _graceful_tree_stop(self, child) -> None:
        pid = int(child.pid)
        if self._platform_name == "nt":
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
            if self._wait_child(child, self.config.stop_grace_sec):
                return
            try:
                child.terminate()
            except Exception:
                pass
            if self._wait_child(child, self.config.kill_grace_sec):
                return
            self._command_runner(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_child(child, self.config.kill_grace_sec)
            return

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            try:
                child.terminate()
            except Exception:
                pass
        if self._wait_child(child, self.config.stop_grace_sec):
            return
        try:
            os.killpg(pid, getattr(signal, "SIGKILL", 9))
        except ProcessLookupError:
            return
        except Exception:
            try:
                child.kill()
            except Exception:
                pass
        self._wait_child(child, self.config.kill_grace_sec)

    def _stop_locked(self) -> dict:
        self._refresh_child_locked()
        child = self._child
        if child is None:
            self._state.update(
                {
                    "supervisor_state": STOPPED,
                    "runtime_state": STOPPED,
                    "runtime_pid": None,
                }
            )
            self._persist()
            response = self.status()
            response["result"] = "already_stopped"
            return response

        self._state.update({"supervisor_state": STOPPING, "runtime_state": STOPPING})
        self._persist()
        self._record("runtime_stop_requested")
        self._graceful_tree_stop(child)
        exit_code = child.poll()
        self._child = None
        self._close_log_handles()
        self._state.update(
            {
                "supervisor_state": STOPPED,
                "runtime_state": STOPPED,
                "runtime_pid": None,
                "runtime_exit_code": exit_code,
                "last_exit_at": _utc_now(),
                "last_failure": None,
            }
        )
        self._persist()
        self._record("runtime_stopped", exit_code=exit_code)
        response = self.status()
        response["result"] = "stopped"
        return response

    def stop(self) -> dict:
        with self._lock:
            return self._stop_locked()

    def restart(self, config_path: str | Path | None = None) -> dict:
        with self._lock:
            requested = config_path or self._state.get("config_path")
            if not requested:
                raise InvalidRuntimeConfig("restart requires a runtime config path")
            self._record("runtime_restart_requested")
            self._stop_locked()
            return self._launch_locked(requested, is_restart=True)

    def _refresh_child_locked(self) -> None:
        child = self._child
        if child is None:
            return
        exit_code = child.poll()
        if exit_code is None:
            return
        self._child = None
        self._close_log_handles()
        self._state.update(
            {
                "runtime_pid": None,
                "runtime_exit_code": int(exit_code),
                "last_exit_at": _utc_now(),
            }
        )
        if int(exit_code) == 0:
            self._state.update(
                {
                    "supervisor_state": STOPPED,
                    "runtime_state": STOPPED,
                    "last_failure": None,
                }
            )
            self._persist()
            self._record("runtime_stopped", exit_code=int(exit_code))
            return

        self._state.update(
            {
                "supervisor_state": FAILED,
                "runtime_state": FAILED,
                "last_failure": f"runtime_exited_with_code_{int(exit_code)}",
            }
        )
        self._persist()
        self._record(
            "runtime_failed",
            exit_code=int(exit_code),
            failure_reason=self._state["last_failure"],
        )
        self._schedule_auto_restart_locked()

    def _schedule_auto_restart_locked(self) -> None:
        if not self.config.auto_restart or self.config.restart_max_attempts <= 0:
            return
        now = time.monotonic()
        while self._restart_attempts and now - self._restart_attempts[0] > self.config.restart_window_sec:
            self._restart_attempts.popleft()
        if len(self._restart_attempts) >= self.config.restart_max_attempts:
            self._record("runtime_restart_limit_reached")
            return
        requested = self._state.get("config_path")
        if not requested:
            return
        self._restart_attempts.append(now)
        self._state.update({"supervisor_state": BACKOFF, "runtime_state": BACKOFF})
        self._persist()
        threading.Thread(
            target=self._auto_restart_after_backoff,
            args=(str(requested),),
            name="runtime-supervisor-backoff",
            daemon=True,
        ).start()

    def _auto_restart_after_backoff(self, config_path: str) -> None:
        if self._monitor_stop.wait(self.config.restart_backoff_sec):
            return
        with self._lock:
            if self._child is not None or self._state["runtime_state"] != BACKOFF:
                return
            try:
                self._launch_locked(config_path, is_restart=True)
            except Exception:
                pass

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self.config.monitor_interval_sec):
            with self._lock:
                if self._state["runtime_state"] != STOPPING:
                    self._refresh_child_locked()

    def status(self) -> dict:
        with self._lock:
            if self._state["runtime_state"] != STOPPING:
                self._refresh_child_locked()
            result = dict(self._state)
            result["ok"] = True
            return result

    def close(self, *, stop_runtime: bool = True) -> None:
        self._monitor_stop.set()
        if stop_runtime:
            try:
                self.stop()
            except Exception:
                pass
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
