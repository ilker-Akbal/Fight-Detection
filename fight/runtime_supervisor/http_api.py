from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .core import InvalidRuntimeConfig


MAX_REQUEST_BYTES = 64 * 1024


def create_http_server(supervisor, *, host: str, port: int, token: str):
    if host != "127.0.0.1":
        raise ValueError("runtime supervisor must bind to 127.0.0.1")
    if not str(token or ""):
        raise ValueError("RUNTIME_SUPERVISOR_TOKEN is required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "FightRuntimeSupervisor/1.0"

        def log_message(self, _format, *_args):
            return

        def _send(self, status: int, payload: dict) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self) -> bool:
            expected = f"Bearer {token}"
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, expected)

        def _body(self) -> dict:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("request body is too large")
            if length == 0:
                return {}
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request JSON root must be an object")
            return payload

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self._send(
                    200,
                    {
                        "ok": True,
                        "service": "runtime_supervisor",
                        "supervisor_pid": supervisor.status()["supervisor_pid"],
                    },
                )
                return
            if path == "/status":
                self._send(200, supervisor.status())
                return
            self._send(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in {"/start", "/stop", "/restart"}:
                self._send(404, {"ok": False, "error": "not_found"})
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = self._body()
                if path == "/start":
                    result = supervisor.start(body.get("config_path", ""))
                elif path == "/stop":
                    result = supervisor.stop()
                else:
                    result = supervisor.restart(body.get("config_path"))
                self._send(200, result)
            except (InvalidRuntimeConfig, ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                self._send(
                    500,
                    {"ok": False, "error": f"supervisor_operation_failed: {type(exc).__name__}"},
                )

    return ThreadingHTTPServer((host, int(port)), Handler)
