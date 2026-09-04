from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

import cv2


LOGGER = logging.getLogger(__name__)
H264_CODEC_NAMES = {"h264", "avc1", "x264"}


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _remove_failed_output(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def build_h264_command(
    executable: str,
    input_args: Sequence[str],
    output_path: str | Path,
) -> list[str]:
    """Build the browser-compatible encoding profile used by the runtime."""
    return [
        str(executable),
        "-y",
        *[str(value) for value in input_args],
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]


def encode_h264_with_ffmpeg(
    input_args: Sequence[str],
    output_path: str | Path,
) -> bool:
    executable = ffmpeg_path()
    if not executable:
        return False

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove_failed_output(target)
    command = build_h264_command(executable, input_args, target)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("ffmpeg H264 encode could not start: %s", exc)
        _remove_failed_output(target)
        return False

    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-500:]
        LOGGER.warning(
            "ffmpeg H264 encode failed (return_code=%s): %s",
            result.returncode,
            detail,
        )
        _remove_failed_output(target)
        return False
    return True


def transcode_to_browser_mp4(input_path: str | Path, output_path: str | Path) -> bool:
    return encode_h264_with_ffmpeg(["-i", str(input_path)], output_path)


def probe_video_codec(path: str | Path) -> dict[str, str]:
    """Best-effort codec probe, with an OpenCV fallback when ffprobe is absent."""
    target = Path(path)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        command = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,codec_tag_string,pix_fmt",
            "-of",
            "json",
            str(target),
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
            streams = payload.get("streams") or []
            if result.returncode == 0 and streams:
                return {
                    "codec_name": str(streams[0].get("codec_name") or "").lower(),
                    "codec_tag_string": str(
                        streams[0].get("codec_tag_string") or ""
                    ).lower(),
                    "pix_fmt": str(streams[0].get("pix_fmt") or "").lower(),
                }
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            pass

    capture = cv2.VideoCapture(str(target))
    try:
        if not capture.isOpened():
            return {}
        value = int(capture.get(cv2.CAP_PROP_FOURCC) or 0)
        tag = "".join(chr((value >> (8 * index)) & 0xFF) for index in range(4))
        return {"codec_name": tag.strip().lower(), "codec_tag_string": tag.lower()}
    finally:
        capture.release()


def is_h264_video(path: str | Path) -> bool:
    info = probe_video_codec(path)
    return bool(
        info.get("codec_name", "").lower() in H264_CODEC_NAMES
        or info.get("codec_tag_string", "").lower() in H264_CODEC_NAMES
    )


def open_opencv_writer(
    output_path: str | Path,
    fps: float,
    frame_size: tuple[int, int],
    codec_codes: Iterable[str],
):
    """Return the first usable writer and codec without retaining failed files."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for code in codec_codes:
        _remove_failed_output(target)
        writer = cv2.VideoWriter(
            str(target),
            cv2.VideoWriter_fourcc(*code),
            float(fps),
            frame_size,
        )
        if writer.isOpened():
            return writer, code
        writer.release()
    return None, ""


def _write_frames_opencv(
    frames_bgr,
    output_path: str | Path,
    fps: float,
    codec_codes: Sequence[str],
) -> str:
    if not frames_bgr:
        return ""
    height, width = frames_bgr[0].shape[:2]
    writer, codec = open_opencv_writer(
        output_path,
        fps,
        (width, height),
        codec_codes,
    )
    if writer is None:
        return ""
    try:
        for frame in frames_bgr:
            if frame is None:
                continue
            if frame.shape[:2] != (height, width):
                frame = cv2.resize(frame, (width, height))
            writer.write(frame)
    finally:
        writer.release()
    target = Path(output_path)
    return codec if target.is_file() and target.stat().st_size > 0 else ""


def save_frames_browser_mp4(frames_bgr, output_path: str | Path, fps: float) -> bool:
    """Prefer H264/yuv420p/faststart and use a bounded OpenCV fallback."""
    if not frames_bgr:
        return False
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if ffmpeg_path():
        with tempfile.TemporaryDirectory() as temp_dir:
            intermediate = Path(temp_dir) / "frames.avi"
            codec = _write_frames_opencv(
                frames_bgr,
                intermediate,
                fps,
                ("MJPG", "XVID"),
            )
            if codec and transcode_to_browser_mp4(intermediate, target):
                return True

    codec = _write_frames_opencv(
        frames_bgr,
        target,
        fps,
        ("avc1", "H264", "X264", "mp4v"),
    )
    if not codec:
        return False
    if codec.lower() not in H264_CODEC_NAMES:
        LOGGER.warning(
            "ffmpeg H264 encoding unavailable; wrote %s with degraded browser compatibility",
            codec,
        )
    return True
