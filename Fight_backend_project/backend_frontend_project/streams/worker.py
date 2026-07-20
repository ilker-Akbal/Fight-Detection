import subprocess
from threading import Thread, Event
from streams.state import upsert_stream

class ProcessWorker(Thread):
    def __init__(self, worker_id: str, command: list, sources: list):
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.command = command
        self.sources = sources
        self.stop_event = Event()
        self.process = None

    def stop(self):
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self):
        upsert_stream(self.worker_id, {
            "stream_id": self.worker_id,
            "sources": self.sources,
            "status": "starting",
            "connected": False,
            "fight": False,
            "confidence": 0.0,
            "message": "Process başlatılıyor"
        })

        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            upsert_stream(self.worker_id, {
                "status": "running",
                "connected": True,
                "message": "Pipeline çalışıyor",
                "pid": self.process.pid
            })

            while not self.stop_event.is_set():
                if self.process.poll() is not None:
                    code = self.process.returncode
                    upsert_stream(self.worker_id, {
                        "status": "stopped" if code == 0 else "error",
                        "connected": False,
                        "message": f"Process kapandı: exit={code}"
                    })
                    return

                upsert_stream(self.worker_id, {
                    "status": "running",
                    "connected": True,
                    "message": "Pipeline aktif"
                })

                self.stop_event.wait(2.0)

        except Exception as exc:
            upsert_stream(self.worker_id, {
                "status": "error",
                "connected": False,
                "message": f"{type(exc).__name__}: {exc}"
            })
        finally:
            if self.process and self.process.poll() is None:
                self.process.terminate()