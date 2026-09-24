"""Parent-owned private preview fan-out. No source handles, disk frames or AI work.

The bounded IPC queue and one JPEG per slot are deliberately lossy. Each viewer
has only a sequence cursor; socket stalls never hold the cache lock.
"""
import hmac
import json
import queue
import secrets
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from fight.operations import atomic_json

MAX_JPEG = 8 * 1024 * 1024


def gateway_descriptor_path():
    # Never put the private bearer token under Django MEDIA_ROOT/static serving.
    return Path(__file__).resolve().parents[2] / ".runtime_supervisor" / "preview_gateway.json"


class PreviewCache:
    def __init__(self, generations, clock=time.perf_counter):
        self.generations, self.clock = generations, clock
        self.frames = {}
        self.condition = threading.Condition()

    def publish(self, packet):
        camera, slot, generation, sequence, captured, jpeg = packet
        if (not 0 <= slot < len(self.generations) or self.generations[slot] != generation
                or not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= MAX_JPEG):
            return False
        with self.condition:
            previous = self.frames.get(slot)
            if previous and previous[1] == generation and previous[2] >= sequence:
                return False
            self.frames[slot] = (camera, generation, sequence, captured, jpeg)
            self.condition.notify_all()
        return True

    def latest(self, camera):
        with self.condition:
            for slot, frame in self.frames.items():
                if (frame[0] == camera and self.generations[slot] == frame[1]
                        and 0 <= self.clock() - frame[3] <= 3):
                    return frame
        return None

    def status(self):
        with self.condition:
            return {frame[0]: {"age_sec": max(0, self.clock() - frame[3]),
                               "generation": frame[1]}
                    for slot, frame in self.frames.items()
                    if self.generations[slot] == frame[1]}


class PreviewGateway:
    def __init__(self, channel, generations, output_dir, run_id, *, descriptor_path=None):
        self.channel = channel
        self.cache = PreviewCache(generations)
        self.stop = threading.Event()
        self.token = secrets.token_urlsafe(32)
        self.clients = threading.BoundedSemaphore(64)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.connection.settimeout(2)
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    self.send_error(403)
                    return
                if self.path == "/status":
                    data = json.dumps(owner.cache.status()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path.startswith("/snapshot/"):
                    frame = owner.cache.latest(unquote(self.path[len("/snapshot/"):]))
                    if frame is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(frame[4])))
                    self.end_headers()
                    self.wfile.write(frame[4])
                    return
                multiplex = self.path.startswith("/frames/")
                if not multiplex and not self.path.startswith("/stream/"):
                    self.send_error(404)
                    return
                cameras = unquote(self.path.split("/", 2)[2]).split(",")
                if not 1 <= len(cameras) <= 12:
                    self.send_error(400)
                    return
                if not owner.clients.acquire(blocking=False):
                    self.send_error(503)
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-camera-frames" if multiplex else
                                     "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    cursors = {}
                    deadline = time.monotonic() + 5
                    while not owner.stop.is_set():
                        for camera in cameras:
                            frame = owner.cache.latest(camera)
                            if frame is not None and (frame[1], frame[2]) != cursors.get(camera):
                                cursors[camera] = frame[1], frame[2]
                                data = frame[4]
                                if multiplex:
                                    self.wfile.write(str(len(data)).encode() + b" " + camera.encode("ascii") + b"\n" + data)
                                else:
                                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                                     + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                                deadline = time.monotonic() + 5
                        if time.monotonic() >= deadline:
                            return  # Do not show a frozen frame indefinitely.
                        with owner.cache.condition:
                            owner.cache.condition.wait(.05)
                except (OSError, ConnectionError):
                    pass  # Viewer disconnected or exceeded its bounded socket wait.
                finally:
                    owner.clients.release()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        # Limit *all* accepted connections, including unauthenticated clients,
        # before ThreadingMixIn creates their threads.
        self.connections = threading.BoundedSemaphore(72)
        original = self.server.process_request
        def process_request(request, address):
            if not self.connections.acquire(False):
                self.server.shutdown_request(request)
                return
            original(request, address)
        original_thread = self.server.process_request_thread
        def process_thread(request, address):
            try:
                request.settimeout(2)
                original_thread(request, address)
            finally:
                self.connections.release()
        self.server.process_request = process_request
        self.server.process_request_thread = process_thread
        descriptor_path = descriptor_path or gateway_descriptor_path()
        atomic_json(descriptor_path, {
            "run_id": run_id, "port": self.server.server_port, "token": self.token,
        })
        descriptor_path.chmod(0o600)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True).start()
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        while not self.stop.is_set():
            try:
                self.cache.publish(self.channel.get(timeout=.1))
            except queue.Empty:
                continue
            except (EOFError, OSError, ValueError):
                return

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
