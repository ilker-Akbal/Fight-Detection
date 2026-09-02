from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SupervisorUnavailable(RuntimeError):
    pass


class SupervisorRequestError(RuntimeError):
    pass


class RuntimeSupervisorClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 3.0):
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token or "")
        self.timeout = max(0.1, float(timeout))

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "request_failed")
            except Exception:
                detail = "request_failed"
            raise SupervisorRequestError(detail) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise SupervisorUnavailable("runtime supervisor is unavailable") from exc

    def health(self) -> dict:
        return self._request("GET", "/health")

    def status(self) -> dict:
        return self._request("GET", "/status")

    def start(self, config_path: str) -> dict:
        return self._request("POST", "/start", {"config_path": str(config_path)})

    def stop(self) -> dict:
        return self._request("POST", "/stop", {})

    def restart(self, config_path: str | None = None) -> dict:
        payload = {"config_path": str(config_path)} if config_path else {}
        return self._request("POST", "/restart", payload)
