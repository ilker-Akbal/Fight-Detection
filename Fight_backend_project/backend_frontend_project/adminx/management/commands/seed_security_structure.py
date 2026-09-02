from django.core.management.base import BaseCommand
from django.db import transaction

from adminx.models import Location, SecurityUnit, SecurityUnitCoverage
from incidents.models import Incident, IncidentRoutingRule


class Command(BaseCommand):
    help = "İdempotent demo lokasyon ağacı ve güvenlik kapsamları oluşturur."

    @transaction.atomic
    def handle(self, *args, **options):
        campus = self._location(
            "demo-campus",
            "Demo Campus",
            Location.TYPE_CAMPUS,
        )
        engineering = self._location(
            "engineering-faculty",
            "Engineering Faculty",
            Location.TYPE_FACULTY,
            campus,
        )
        block_a = self._location(
            "engineering-block-a",
            "A Block",
            Location.TYPE_BUILDING,
            engineering,
        )
        first_floor = self._location(
            "engineering-block-a-floor-1",
            "1st Floor",
            Location.TYPE_FLOOR,
            block_a,
        )
        self._location(
            "engineering-block-a-corridor-a",
            "Corridor A",
            Location.TYPE_CORRIDOR,
            first_floor,
        )
        self._location(
            "demo-campus-main-entrance",
            "Main Entrance",
            Location.TYPE_ENTRANCE,
            campus,
        )

        central, _ = SecurityUnit.objects.update_or_create(
            code="central-security",
            defaults={
                "name": "Central Security",
                "unit_type": SecurityUnit.TYPE_CENTRAL,
                "location": campus,
                "active": True,
                "is_central": True,
            },
        )
        faculty, _ = SecurityUnit.objects.update_or_create(
            code="engineering-faculty-security",
            defaults={
                "name": "Engineering Faculty Security",
                "unit_type": SecurityUnit.TYPE_FACULTY,
                "location": engineering,
                "active": True,
                "is_central": False,
            },
        )
        block, _ = SecurityUnit.objects.update_or_create(
            code="engineering-block-a-security",
            defaults={
                "name": "A Block Security",
                "unit_type": SecurityUnit.TYPE_LOCAL,
                "location": block_a,
                "active": True,
                "is_central": False,
            },
        )

        self._coverage(central, campus)
        self._coverage(faculty, engineering)
        self._coverage(block, block_a)
        self._routing_rule("A Block Fight Stage 0", block, 0, 0, 10)
        self._routing_rule("Engineering Fight Stage 1", faculty, 1, 30, 20)
        self._routing_rule("Central Fight Stage 2", central, 2, 60, 30)

        self.stdout.write(
            self.style.SUCCESS(
                "Demo güvenlik yapısı hazır: 6 lokasyon, 3 birim, 3 kapsam, 3 FIGHT routing kuralı."
            )
        )

    @staticmethod
    def _location(code, name, location_type, parent=None):
        location, _ = Location.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "location_type": location_type,
                "parent": parent,
                "active": True,
            },
        )
        return location

    @staticmethod
    def _coverage(unit, location):
        active = SecurityUnitCoverage.objects.filter(
            security_unit=unit,
            location=location,
            active=True,
        ).first()
        if active is not None:
            if not active.include_descendants:
                active.include_descendants = True
                active.save(update_fields=["include_descendants", "updated_at"])
            return active
        return SecurityUnitCoverage.objects.create(
            security_unit=unit,
            location=location,
            include_descendants=True,
            active=True,
        )

    @staticmethod
    def _routing_rule(name, unit, stage, delay_sec, priority):
        rule, _ = IncidentRoutingRule.objects.update_or_create(
            security_unit=unit,
            incident_type=Incident.TYPE_FIGHT,
            routing_stage=stage,
            active=True,
            defaults={
                "name": name,
                "delay_sec": delay_sec,
                "priority": priority,
            },
        )
        return rule
