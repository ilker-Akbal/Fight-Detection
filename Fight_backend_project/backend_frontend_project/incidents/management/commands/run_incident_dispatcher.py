import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from fight.service_loop import run_service
from fight.runtime_supervisor.locking import SingletonLockError

from incidents.services.ingest import dispatcher_tick


class Command(BaseCommand):
    help = "Durable incident outbox'u ingest eder ve due routing/escalation kurallarını işler."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Tek tick çalıştır ve çık.")
        parser.add_argument(
            "--outbox",
            default=str(settings.INCIDENT_OUTBOX_PATH),
            help="Incident JSONL outbox yolu.",
        )
        parser.add_argument("--poll-interval", type=float, default=1.0)

    def handle(self, *args, **options):
        poll_interval = float(options["poll_interval"])
        if poll_interval < 0.1:
            raise CommandError("--poll-interval en az 0.1 saniye olmalıdır.")
        outbox = Path(options["outbox"]).resolve()

        def tick():
            close_old_connections()
            try:
                return dispatcher_tick(outbox)
            finally:
                close_old_connections()

        try:
            run_service(tick, settings.OPERATIONAL_SERVICE_DIR / "dispatcher.lock",
                        once=options["once"], interval=poll_interval,
                        report=lambda row: self.stdout.write(json.dumps(row, sort_keys=True)))
        except SingletonLockError as exc:
            raise CommandError("Incident dispatcher is already running") from exc
