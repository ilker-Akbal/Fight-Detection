from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from fight.service_loop import run_service
from fight.runtime_supervisor.locking import SingletonLockError

from services.pipeline_bridge.camera_registry import CameraRegistryReconciler


class Command(BaseCommand):
    help = "Publish the Django fight-camera registry to the Runtime Supervisor."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=settings.CAMERA_REGISTRY_POLL_INTERVAL_SEC,
        )

    def handle(self, *args, **options):
        poll_interval = float(options["poll_interval"])
        if poll_interval < 0.5:
            raise CommandError("--poll-interval must be at least 0.5 seconds")
        reconciler = CameraRegistryReconciler()
        self.stdout.write("Camera registry reconciler started")
        def tick():
            close_old_connections()
            try:
                result = reconciler.tick()
                return result if result["changed"] or options["once"] else {}
            finally:
                close_old_connections()

        try:
            run_service(tick, settings.OPERATIONAL_SERVICE_DIR / "reconciler.lock",
                        once=options["once"], interval=poll_interval,
                        report=lambda row: self.stdout.write(json.dumps(row, sort_keys=True)))
        except SingletonLockError as exc:
            raise CommandError("Camera registry reconciler is already running") from exc
