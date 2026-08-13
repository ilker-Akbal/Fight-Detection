import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from fight.pipeline.incident_aggregator import IncidentAggregator


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _has_libx264() -> bool:
    if not FFMPEG:
        return False
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.returncode == 0 and b"libx264" in result.stdout


FFMPEG_TESTS_AVAILABLE = bool(FFMPEG and FFPROBE and _has_libx264())


class IncidentOverlayFallbackTests(unittest.TestCase):
    def test_missing_ffmpeg_preserves_existing_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final = root / "incident.mp4"
            original = b"known-good-concat"
            final.write_bytes(original)
            agg = IncidentAggregator(str(root / "incidents"), clip_ready_wait_sec=0)
            try:
                with mock.patch(
                    "fight.pipeline.incident_aggregator.shutil.which",
                    return_value=None,
                ):
                    self.assertFalse(agg._add_ai_overlay_to_clip(final, {}))
                self.assertEqual(final.read_bytes(), original)
                self.assertEqual(list(root.glob(".*__overlay_*.mp4")), [])
            finally:
                agg.close_all()

    def test_ffmpeg_start_failure_preserves_existing_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            final = root / "incident.mp4"
            original = b"known-good-concat"
            final.write_bytes(original)
            agg = IncidentAggregator(str(root / "incidents"), clip_ready_wait_sec=0)
            capture = mock.Mock()
            capture.isOpened.return_value = True
            capture.get.side_effect = (10.0, 160, 120)
            try:
                with (
                    mock.patch(
                        "fight.pipeline.incident_aggregator.shutil.which",
                        return_value="ffmpeg",
                    ),
                    mock.patch(
                        "fight.pipeline.incident_aggregator.cv2.VideoCapture",
                        return_value=capture,
                    ),
                    mock.patch(
                        "fight.pipeline.incident_aggregator.subprocess.Popen",
                        side_effect=OSError("synthetic ffmpeg start failure"),
                    ),
                ):
                    self.assertFalse(agg._add_ai_overlay_to_clip(final, {}))
                self.assertEqual(final.read_bytes(), original)
                self.assertEqual(list(root.glob(".*__overlay_*.mp4")), [])
            finally:
                agg.close_all()


@unittest.skipUnless(
    FFMPEG_TESTS_AVAILABLE,
    "ffmpeg, ffprobe, and the libx264 encoder are required",
)
@pytest.mark.ffmpeg
@pytest.mark.integration
class IncidentOverlayCodecTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.agg = IncidentAggregator(str(self.root / "incidents"), clip_ready_wait_sec=0)

    def tearDown(self):
        self.agg.close_all()
        self.temporary.cleanup()

    def segment(self, path: Path, color: str) -> None:
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s=160x120:r=10:d=0.5", "-an",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             str(path)],
            check=True,
        )

    def probe(self, path: Path) -> dict:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt:format=duration",
             "-of", "json", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return json.loads(result.stdout)

    def test_three_part_concat_and_overlay_remain_browser_h264(self):
        parts = []
        for index, color in enumerate(("red", "green", "blue")):
            path = self.root / f"part-{index}.mp4"
            self.segment(path, color)
            parts.append(str(path))
        final = self.root / "incident.mp4"
        self.assertTrue(self.agg._concat_mp4s(parts, final))
        with mock.patch.dict(
            os.environ,
            {"INCIDENT_POSE_MODEL": str(self.root / "missing-pose-model.pt")},
        ):
            self.assertTrue(self.agg._add_ai_overlay_to_clip(final, {"camera_id": "test"}))
        probe = self.probe(final)
        self.assertEqual(probe["streams"][0]["codec_name"], "h264")
        self.assertEqual(probe["streams"][0]["pix_fmt"], "yuv420p")
        self.assertGreater(float(probe["format"]["duration"]), 0)
        self.assertGreater(final.stat().st_size, 0)

    def test_overlay_encoder_failure_preserves_existing_concat(self):
        part = self.root / "part.mp4"
        self.segment(part, "red")
        final = self.root / "incident.mp4"
        self.assertTrue(self.agg._concat_mp4s([str(part)], final))
        original = final.read_bytes()
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("synthetic failure")):
            self.assertFalse(self.agg._add_ai_overlay_to_clip(final, {}))
        self.assertEqual(final.read_bytes(), original)
        self.assertTrue(self.agg._validate_browser_mp4(final))

    def test_overlay_validation_failure_preserves_existing_concat(self):
        part = self.root / "part.mp4"
        self.segment(part, "red")
        final = self.root / "incident.mp4"
        self.assertTrue(self.agg._concat_mp4s([str(part)], final))
        original = final.read_bytes()
        with mock.patch.object(self.agg, "_validate_browser_mp4", return_value=False):
            self.assertFalse(self.agg._add_ai_overlay_to_clip(final, {}))
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".*__overlay_*.mp4")), [])


if __name__ == "__main__":
    unittest.main()
