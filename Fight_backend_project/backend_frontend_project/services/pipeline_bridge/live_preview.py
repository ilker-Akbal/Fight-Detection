"""Authenticated web relay of the current runtime's private JPEG fan-out.

This module cannot open a physical camera. The private endpoint/token never
leaves the backend. Both run identity and output containment are checked.
"""
import http.client
import json
import time
from pathlib import Path
from urllib.parse import quote

from django.conf import settings

from .fight_runner import get_active_run, get_pipeline_status
from fight.pipeline_mp.preview_gateway import gateway_descriptor_path


def gateway_context():
    status = get_pipeline_status()
    if status.get("runtime_state") not in {"RUNNING", "STARTING"} or status.get("orphan_detected"):
        return None
    active = get_active_run(status)
    if active is None:
        return None
    try:
        root = Path(active.run_dir).resolve()
        root.relative_to(Path(settings.PIPELINE_OUTPUT_BASE).resolve())
        descriptor = json.loads(gateway_descriptor_path().read_text(encoding="utf-8"))
        if (descriptor["run_id"] != str(active.run_id)
                or not 1 <= int(descriptor["port"]) <= 65535
                or not isinstance(descriptor["token"], str)):
            return None
        return descriptor
    except (OSError, ValueError, KeyError, TypeError):
        return None


def connect(context, path):
    connection = http.client.HTTPConnection("127.0.0.1", int(context["port"]), timeout=3)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + context["token"]})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError("Preview unavailable")
        return connection, response
    except (OSError, http.client.HTTPException):
        connection.close()
        raise


def preview_status():
    context = gateway_context()
    if context is None:
        return {}
    connection = None
    try:
        connection, response = connect(context, "/status")
        return json.loads(response.read(256 * 1024))
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    finally:
        if connection:
            connection.close()


def snapshot(camera_id):
    context = gateway_context()
    if context is None:
        return None
    connection = None
    try:
        connection, response = connect(context, "/snapshot/" + quote(camera_id, safe=""))
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) <= 8 * 1024 * 1024 and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
            return data
    except (OSError, http.client.HTTPException):
        return None
    finally:
        if connection:
            connection.close()


def stream(camera_id, authorized=lambda: True, *, multiplex=False):
    context = gateway_context()
    if context is None:
        return
    connection = None
    next_check = 0
    try:
        connection, response = connect(context, ("/frames/" if multiplex else "/stream/") + quote(camera_id, safe=""))
        while True:
            if time.monotonic() >= next_check:
                if not authorized() or gateway_context() != context:
                    return
                next_check = time.monotonic() + 2
            chunk = response.read1(64 * 1024)
            if not chunk:
                return
            yield chunk
    except (OSError, http.client.HTTPException):
        return
    finally:
        if connection:
            connection.close()
