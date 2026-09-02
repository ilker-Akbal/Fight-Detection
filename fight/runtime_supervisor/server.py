from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, timezone

from .core import RuntimeSupervisor, SupervisorConfig
from .http_api import create_http_server
from .locking import SingletonLock, SingletonLockError


def parse_args():
    parser = argparse.ArgumentParser(description="Fight runtime supervisor")
    parser.add_argument(
        "--host",
        default=os.getenv("RUNTIME_SUPERVISOR_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("RUNTIME_SUPERVISOR_PORT", "8765")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = SupervisorConfig.from_env()
    token = os.getenv("RUNTIME_SUPERVISOR_TOKEN", "")
    singleton = SingletonLock(config.state_dir / "supervisor.lock")
    try:
        singleton.acquire()
    except SingletonLockError as exc:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(
                config.state_dir / "supervisor_events.jsonl",
                "a",
                encoding="utf-8",
            ) as handle:
                handle.write(
                    json.dumps(
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "detail": "singleton_lock_failed",
                            "supervisor_pid": os.getpid(),
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass
        print(f"[SUPERVISOR] singleton_lock_failed: {exc}", file=sys.stderr, flush=True)
        return 2

    supervisor = None
    try:
        supervisor = RuntimeSupervisor(config)
        server = create_http_server(
            supervisor,
            host=args.host,
            port=args.port,
            token=token,
        )

        def request_shutdown(_signum, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, request_shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_shutdown)
        print(
            f"[SUPERVISOR] listening on http://{args.host}:{args.port}",
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    finally:
        if supervisor is not None:
            supervisor.close(stop_runtime=True)
        singleton.release()


if __name__ == "__main__":
    raise SystemExit(main())
