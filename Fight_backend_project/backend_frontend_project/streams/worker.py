from threading import Event, Thread

from streams.manager import get_status, start_sources, stop_sources
from streams.state import upsert_stream


class ProcessWorker(Thread):
    """Legacy thread facade; process ownership remains in Runtime Supervisor."""

    def __init__(self, worker_id: str, command: list, sources: list):
        super().__init__(daemon=True)
        self.worker_id = worker_id
        self.command = command  # Retained for API compatibility; never executed here.
        self.sources = sources
        self.stop_event = Event()
        self.process = None

    def stop(self):
        self.stop_event.set()
        stop_sources()

    def run(self):
        upsert_stream(
            self.worker_id,
            {
                "stream_id": self.worker_id,
                "sources": self.sources,
                "status": "starting",
                "connected": False,
                "fight": False,
                "confidence": 0.0,
                "message": "Supervisor start isteği gönderiliyor",
            },
        )
        try:
            report = start_sources(self.sources)
            upsert_stream(
                self.worker_id,
                {
                    "status": "running",
                    "connected": True,
                    "message": "Pipeline çalışıyor",
                    "pid": report.get("pid"),
                },
            )
            while not self.stop_event.wait(2.0):
                report = get_status()
                running = report.get("running")
                if running is not True:
                    upsert_stream(
                        self.worker_id,
                        {
                            "status": "stopped" if running is False else "degraded",
                            "connected": False,
                            "message": (
                                "Pipeline kapandı"
                                if running is False
                                else "Runtime supervisor ulaşılamıyor"
                            ),
                        },
                    )
                    return
                upsert_stream(
                    self.worker_id,
                    {
                        "status": "running",
                        "connected": True,
                        "message": "Pipeline aktif",
                    },
                )
        except Exception as exc:
            upsert_stream(
                self.worker_id,
                {
                    "status": "error",
                    "connected": False,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )
