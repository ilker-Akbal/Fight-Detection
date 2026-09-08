import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, override_settings

from services.pipeline_bridge import common_preview
from speed_detection.views import speed_camera_stream


class SourceOwnershipTests(SimpleTestCase):
    def test_stream_reads_only_common_preview_and_closes_on_ownership_loss(self):
        with tempfile.TemporaryDirectory() as temporary, override_settings(PIPELINE_OUTPUT_BASE=temporary):
            root = Path(temporary)
            run = root / "run"
            (run / "previews").mkdir(parents=True)
            jpeg = b"\xff\xd8fixture\xff\xd9"
            (run / "previews" / "speed.jpg").write_bytes(jpeg)
            active = SimpleNamespace(run_dir=run, run_id="run")
            item = SimpleNamespace(camera=SimpleNamespace(camera_id="speed", source="rtsp://private/source"))
            request = RequestFactory().get("/speed/stream/speed/")
            request.user = SimpleNamespace(is_authenticated=True)
            with patch("speed_detection.views._get_speed_config_by_camera_id", return_value=item) as access, patch(
                "cv2.VideoCapture", side_effect=AssertionError("second source open")), patch(
                "speed_detection.views._open_camera_source", side_effect=AssertionError("second source open")), patch.object(
                common_preview, "get_pipeline_status", return_value={"runtime_state": "RUNNING"}) as status, patch.object(
                common_preview, "get_active_run", return_value=active), patch.object(common_preview.time, "sleep"):
                response = speed_camera_stream(request, "speed")
                assert response.status_code == 200
                assert response["Content-Type"] == "multipart/x-mixed-replace; boundary=frame"
                iterator = iter(response.streaming_content)
                with patch.object(common_preview.time, "monotonic", side_effect=[10, 12]):
                    assert jpeg in next(iterator)
                    status.return_value = {"runtime_state": "UNKNOWN"}
                    with self.assertRaises(StopIteration):
                        next(iterator)
                response.close()
                access.assert_called_once_with("speed", request.user)
                for state in ("UNKNOWN", "STOPPED", "STOPPING", "FAILED"):
                    status.return_value = {"runtime_state": state}
                    with self.assertRaises(Http404):
                        speed_camera_stream(request, "speed")
                status.return_value = {"runtime_state": "RUNNING", "orphan_detected": True}
                with self.assertRaises(Http404):
                    speed_camera_stream(request, "speed")
                status.return_value = {"runtime_state": "RUNNING"}
                assert common_preview.preview_context("../../escape") is None
                (run / "previews" / "speed.jpg").unlink()
                with self.assertRaises(Http404):
                    speed_camera_stream(request, "speed")
