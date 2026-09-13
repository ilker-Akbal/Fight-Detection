"""Local generated MJPEG fixture, never evidence of real RTSP/network behavior."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class GeneratedLiveSource:
    label = "LIVE-LIKE / SYNTHETIC SOURCE"

    def __init__(self, fps=10):
        import cv2
        import numpy as np
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active = self.peak = self.connections = 0
        # Encode once. No camera/file is opened by this source fixture.
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        cv2.rectangle(frame, (160, 90), (480, 270), (120, 120, 120), -1)
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            raise RuntimeError("generated_source_encode_failed")
        packet = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                  str(len(encoded)).encode() + b"\r\n\r\n" + encoded.tobytes() + b"\r\n")
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.connection.settimeout(2)
                with owner.lock:
                    # Bound server work even if an unexpected client connects.
                    if owner.active >= 4:
                        self.send_error(503)
                        return
                    owner.active += 1
                    owner.connections += 1
                    owner.peak = max(owner.peak, owner.active)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    while not owner.stop.is_set():
                        self.wfile.write(packet)
                        self.wfile.flush()
                        owner.stop.wait(1 / max(1, min(30, fps)))
                except (OSError, TimeoutError):
                    pass
                finally:
                    with owner.lock:
                        owner.active -= 1
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .1}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/generated.mjpeg"

    def __exit__(self, *_args):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
