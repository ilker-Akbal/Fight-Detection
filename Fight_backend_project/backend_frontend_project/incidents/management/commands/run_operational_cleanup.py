import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from fight.service_loop import run_service
from fight.runtime_supervisor.locking import SingletonLockError
from incidents.services.retention import cleanup_tick


class Command(BaseCommand):
    help = "Bounded cleanup of closed operational artifacts; evidence is protected by default."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=3600)

    def handle(self, *args, **options):
        if options["poll_interval"] < 60:
            raise CommandError("--poll-interval must be at least 60 seconds")

        def tick():
            close_old_connections()
            try:
                return cleanup_tick(dry_run=options["dry_run"])
            finally:
                close_old_connections()

        try:
            run_service(tick, settings.OPERATIONAL_SERVICE_DIR / "cleanup.lock",
                        once=options["once"], interval=options["poll_interval"],
                        report=lambda row: self.stdout.write(json.dumps(row, sort_keys=True)))
        except SingletonLockError as exc:
            raise CommandError("Operational cleanup or runtime launch is already running") from exc
