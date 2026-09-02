import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

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

        self.stdout.write(f"Incident dispatcher başladı: outbox={outbox}")
        while True:
            result = dispatcher_tick(outbox)
            if any(result.values()):
                self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if options["once"]:
                return
            try:
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                self.stdout.write("Incident dispatcher durduruldu.")
                return
