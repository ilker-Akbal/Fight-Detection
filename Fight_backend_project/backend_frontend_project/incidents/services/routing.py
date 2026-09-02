from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from adminx.models import Location, SecurityUnit
from incidents.models import (
    Incident,
    IncidentAuditEvent,
    IncidentRoute,
    IncidentRoutingRule,
)
from services.access_scope import build_location_security_unit_index


def _camera_location_id(camera, locations_by_code: dict[str, int]) -> int | None:
    if camera.location_id:
        return camera.location_id
    if camera.faculty:
        return locations_by_code.get(str(camera.faculty))
    return None


def _audit_route(route: IncidentRoute, escalated: bool) -> None:
    IncidentAuditEvent.objects.create(
        incident=route.incident,
        action=(
            IncidentAuditEvent.ACTION_ESCALATED
            if escalated
            else IncidentAuditEvent.ACTION_ROUTED
        ),
        security_unit=route.security_unit,
        routing_stage=route.routing_stage,
        metadata={
            "route_id": route.pk,
            "reason": route.reason,
            "routing_rule_id": route.routing_rule_id,
        },
    )


@transaction.atomic
def route_incident(
    incident: Incident,
    *,
    now=None,
    eligible_rules: list[IncidentRoutingRule] | None = None,
    eligible_central_units: list[SecurityUnit] | None = None,
) -> int:
    now = now or timezone.now()
    incident = (
        Incident.objects
        .select_for_update()
        .select_related("camera", "camera__location")
        .get(pk=incident.pk)
    )
    if incident.status != Incident.STATUS_OPEN:
        return 0
    if incident.routing_started_at is None:
        incident.routing_started_at = now
        incident.save(update_fields=["routing_started_at", "updated_at"])

    if eligible_rules is None or eligible_central_units is None:
        locations = {row.code: row.pk for row in Location.objects.filter(active=True)}
        location_id = _camera_location_id(incident.camera, locations)
        coverage_index = build_location_security_unit_index([location_id])
        unit_ids = coverage_index.get(location_id, set())
        eligible_rules = list(
            IncidentRoutingRule.objects
            .select_related("security_unit")
            .filter(
                active=True,
                incident_type=incident.incident_type,
                security_unit_id__in=unit_ids,
                security_unit__active=True,
            )
            .order_by("routing_stage", "priority", "pk")
        )
        eligible_central_units = list(
            SecurityUnit.objects.filter(pk__in=unit_ids, active=True, is_central=True)
        )

    created_count = 0
    if not eligible_rules:
        for unit in eligible_central_units:
            route, created = IncidentRoute.objects.get_or_create(
                incident=incident,
                security_unit=unit,
                routing_stage=0,
                defaults={
                    "routed_at": now,
                    "reason": "central_fallback",
                    "metadata": {"fallback": True},
                },
            )
            if created:
                created_count += 1
                _audit_route(route, escalated=False)

        new_state = Incident.ROUTING_ROUTED if eligible_central_units else Incident.ROUTING_UNROUTED
        if incident.routing_state != new_state:
            incident.routing_state = new_state
            incident.save(update_fields=["routing_state", "updated_at"])
            if new_state == Incident.ROUTING_UNROUTED:
                IncidentAuditEvent.objects.create(
                    incident=incident,
                    action=IncidentAuditEvent.ACTION_ROUTING_FAILED,
                    metadata={"reason": "no_eligible_routing_rule_or_central_fallback"},
                )
        return created_count

    due_rules = [
        rule for rule in eligible_rules
        if incident.routing_started_at + timedelta(seconds=rule.delay_sec) <= now
    ]
    for rule in due_rules:
        route, created = IncidentRoute.objects.get_or_create(
            incident=incident,
            security_unit=rule.security_unit,
            routing_stage=rule.routing_stage,
            defaults={
                "routed_at": now,
                "routing_rule": rule,
                "reason": "routing_rule_due",
                "metadata": {"delay_sec": rule.delay_sec, "priority": rule.priority},
            },
        )
        if created:
            created_count += 1
            _audit_route(route, escalated=rule.routing_stage > 0)

    new_state = (
        Incident.ROUTING_ROUTED
        if IncidentRoute.objects.filter(incident=incident).exists()
        else Incident.ROUTING_PENDING
    )
    if incident.routing_state != new_state:
        incident.routing_state = new_state
        incident.save(update_fields=["routing_state", "updated_at"])
    return created_count


def process_due_routes(*, now=None, batch_size: int = 200) -> int:
    """Resolve due routes with bulk-loaded rules and coverage topology."""

    now = now or timezone.now()
    incidents = list(
        Incident.objects
        .filter(status=Incident.STATUS_OPEN)
        .select_related("camera", "camera__location")
        .order_by("routing_started_at", "pk")[:batch_size]
    )
    if not incidents:
        return 0

    all_locations = list(Location.objects.filter(active=True).values("pk", "code"))
    locations_by_code = {row["code"]: row["pk"] for row in all_locations}
    incident_location_ids = {
        incident.pk: _camera_location_id(incident.camera, locations_by_code)
        for incident in incidents
    }
    coverage_index = build_location_security_unit_index(incident_location_ids.values())

    rules_by_type_and_unit = defaultdict(list)
    rules = list(
        IncidentRoutingRule.objects
        .select_related("security_unit")
        .filter(active=True, security_unit__active=True)
        .order_by("routing_stage", "priority", "pk")
    )
    for rule in rules:
        rules_by_type_and_unit[(rule.incident_type, rule.security_unit_id)].append(rule)
    central_units = {
        unit.pk: unit
        for unit in SecurityUnit.objects.filter(active=True, is_central=True)
    }

    created_count = 0
    for incident in incidents:
        unit_ids = coverage_index.get(incident_location_ids[incident.pk], set())
        eligible_rules = []
        for unit_id in unit_ids:
            eligible_rules.extend(rules_by_type_and_unit[(incident.incident_type, unit_id)])
        eligible_rules.sort(key=lambda rule: (rule.routing_stage, rule.priority, rule.pk))
        eligible_central = [central_units[unit_id] for unit_id in unit_ids if unit_id in central_units]
        created_count += route_incident(
            incident,
            now=now,
            eligible_rules=eligible_rules,
            eligible_central_units=eligible_central,
        )
    return created_count
