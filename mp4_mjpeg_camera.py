from __future__ import annotations

import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2


class LatestFrame:
    def __init__(self):
        self._lock = threading.Lock()
        self._sequence = 0
        self._jpeg: bytes | None = None

    def update(self, jpeg: bytes):
        with self._lock:
            self._sequence += 1
            self._jpeg = jpeg

    def get(self):
        with self._lock:
            return self._sequence, self._jpeg


class VideoCamera:
    def __init__(
        self,
        video_path: str,
        latest: LatestFrame,
        *,
        jpeg_quality: int = 80,
        loop: bool = True,
    ):
        self.video_path = str(Path(video_path).resolve())
        self.latest = latest
        self.jpeg_quality = int(jpeg_quality)
        self.loop = bool(loop)

        self.running = False
        self.thread: threading.Thread | None = None

    def start(self):
        self.running = True

        self.thread = threading.Thread(
            target=self._run,
            name="mp4-live-camera",
            daemon=True,
        )
        self.thread.start()

    def stop(self):
        self.running = False

        if self.thread is not None:
            self.thread.join(timeout=3.0)

    def _run(self):
        cap = cv2.VideoCapture(self.video_path)

        if not cap.isOpened():
            print(f"[ERROR] Video açılamadı: {self.video_path}")
            self.running = False
            return

        source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)

        if source_fps <= 1.0 or source_fps > 120.0:
            source_fps = 25.0

        frame_interval = 1.0 / source_fps

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        print()
        print("[CAMERA] Video:", self.video_path)
        print("[CAMERA] Çözünürlük:", f"{width}x{height}")
        print("[CAMERA] Kaynak FPS:", round(source_fps, 2))
        print("[CAMERA] Frame sayısı:", frame_count)
        print("[CAMERA] Gerçek zamanlı yayın başladı.")
        print()

        next_frame_at = time.monotonic()

        published = 0
        stats_started = time.monotonic()

        try:
            while self.running:
                ok, frame = cap.read()

                if not ok or frame is None:
                    if not self.loop:
                        print("[CAMERA] Video sona erdi.")
                        break

                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

                    next_frame_at = time.monotonic()

                    print("[CAMERA] Video başa sarıldı.")
                    continue

                # Kaynak FPS'e göre gerçek zamanlı pace.
                now = time.monotonic()

                sleep_for = next_frame_at - now

                if sleep_for > 0:
                    time.sleep(sleep_for)

                # Eğer bilgisayar çok geriye düştüyse frame'leri
                # aşırı hızlı basarak yetişmeye çalışma.
                now = time.monotonic()

                if now - next_frame_at > 1.0:
                    next_frame_at = now

                next_frame_at += frame_interval

                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        self.jpeg_quality,
                    ],
                )

                if not ok:
                    continue

                self.latest.update(encoded.tobytes())

                published += 1

                stats_now = time.monotonic()
                stats_elapsed = stats_now - stats_started

                if stats_elapsed >= 2.0:
                    actual_fps = published / stats_elapsed

                    print(
                        f"[CAMERA] Yayın FPS: {actual_fps:.1f} "
                        f"(hedef {source_fps:.1f})"
                    )

                    published = 0
                    stats_started = stats_now

        finally:
            cap.release()
            print("[CAMERA] Yayın durdu.")


def make_handler(latest: LatestFrame):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path in {"/", "/index.html"}:
                html = """<!doctype html>
<html lang="tr">
<head>
    <meta charset="utf-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <title>Live Camera Test</title>

    <style>
        html, body {
            width: 100%;
            height: 100%;
            margin: 0;

            background: #111;
            color: #fff;

            font-family: Arial, sans-serif;
        }

        body {
            display: flex;
            flex-direction: column;

            align-items: center;
            justify-content: center;

            gap: 12px;
        }

        img {
            width: min(96vw, 1280px);
            max-height: 88vh;

            object-fit: contain;

            background: #000;
        }

        .info {
            opacity: .75;
            font-size: 14px;
        }
    </style>
</head>

<body>

    <img src="/stream.mjpg">

    <div class="info">
        MP4 → gerçek zamanlı pace → JPEG RAM → MJPEG
    </div>

</body>
</html>
""".encode("utf-8")

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "text/html; charset=utf-8",
                )

                self.send_header(
                    "Content-Length",
                    str(len(html)),
                )

                self.send_header(
                    "Cache-Control",
                    "no-store",
                )

                self.end_headers()

                self.wfile.write(html)

                return

            if self.path == "/stream.mjpg":
                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=frame",
                )

                self.send_header(
                    "Cache-Control",
                    "no-store, no-cache, must-revalidate",
                )

                self.send_header(
                    "Pragma",
                    "no-cache",
                )

                self.end_headers()

                last_sequence = -1

                try:
                    while True:
                        sequence, jpeg = latest.get()

                        if (
                            jpeg is None
                            or sequence == last_sequence
                        ):
                            time.sleep(0.002)
                            continue

                        last_sequence = sequence

                        self.wfile.write(b"--frame\r\n")

                        self.wfile.write(
                            b"Content-Type: image/jpeg\r\n"
                        )

                        self.wfile.write(
                            (
                                f"Content-Length: {len(jpeg)}"
                                "\r\n\r\n"
                            ).encode()
                        )

                        self.wfile.write(jpeg)

                        self.wfile.write(b"\r\n")

                        self.wfile.flush()

                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    ConnectionAbortedError,
                ):
                    pass

                return

            self.send_error(404)

    return Handler


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "video",
        help="Yayınlanacak MP4/video dosyası",
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8090,
    )

    parser.add_argument(
        "--quality",
        type=int,
        default=80,
    )

    parser.add_argument(
        "--no-loop",
        action="store_true",
    )

    args = parser.parse_args()

    video_path = Path(args.video)

    if not video_path.exists():
        raise FileNotFoundError(
            f"Video bulunamadı: {video_path}"
        )

    latest = LatestFrame()

    camera = VideoCamera(
        str(video_path),
        latest,
        jpeg_quality=args.quality,
        loop=not args.no_loop,
    )

    camera.start()

    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(latest),
    )

    print()
    print("HTTP kamera çalışıyor.")
    print()
    print(
        f"Tarayıcı: http://127.0.0.1:{args.port}/"
    )
    print(
        f"Akış:     http://127.0.0.1:{args.port}/stream.mjpg"
    )
    print()
    print("Kapatmak için Ctrl+C")
    print()

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nKapatılıyor...")

    finally:
        server.shutdown()
        server.server_close()

        camera.stop()


if __name__ == "__main__":
    main()
