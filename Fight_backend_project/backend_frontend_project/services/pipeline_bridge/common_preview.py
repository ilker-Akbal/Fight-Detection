"""Read-only MJPEG fan-out of the CameraIngest-owned, atomically replaced JPEG."""
import time
from pathlib import Path

from django.conf import settings

from .fight_runner import get_active_run, get_pipeline_status


def preview_context(camera_id):
    status = get_pipeline_status()
    if status.get("runtime_state") not in {"STARTING", "RUNNING"} or status.get("orphan_detected"):
        return None
    active = get_active_run(status)
    if active is None:
        return None
    run_dir = Path(active.run_dir).resolve()
    preview_root = (run_dir / "previews").resolve()
    path = (preview_root / f"{camera_id}.jpg").resolve()
    try:
        run_dir.relative_to(Path(settings.PIPELINE_OUTPUT_BASE).resolve())
        path.relative_to(preview_root)
    except ValueError:
        return None
    return (str(active.run_id), str(run_dir)), path


def preview_mjpeg(camera_id, run_token, path):
    last_signature, next_check = None, 0.0
    while True:
        now = time.monotonic()
        if now >= next_check:
            context = preview_context(camera_id)
            if context is None or context[0] != run_token:
                return
            next_check = now + 1.0
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature != last_signature and 0 < stat.st_size <= 8 * 1024 * 1024:
                with path.open("rb") as source:
                    data = source.read(8 * 1024 * 1024 + 1)
                if len(data) <= 8 * 1024 * 1024 and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
                    last_signature = signature
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-cache\r\n\r\n" + data + b"\r\n"
        except OSError:
            pass  # Atomic replacement/startup gap; never open a physical source.
        time.sleep(0.05)
