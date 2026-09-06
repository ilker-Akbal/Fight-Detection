from __future__ import annotations

import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fight.runtime_supervisor.client import SupervisorRequestError, SupervisorUnavailable
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
        while True:
            try:
                result = reconciler.tick()
                if result["changed"] or options["once"]:
                    self.stdout.write(json.dumps(result, sort_keys=True))
            except (SupervisorUnavailable, SupervisorRequestError) as exc:
                self.stderr.write(f"Supervisor unavailable: {type(exc).__name__}")
            if options["once"]:
                return
            try:
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                self.stdout.write("Camera registry reconciler stopped")
                return
