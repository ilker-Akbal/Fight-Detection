from __future__ import annotations

from django.db.models import QuerySet

from incidents.models import Incident, IncidentRoute
from services.access_scope import (
    get_user_accessible_cameras,
    get_user_security_units,
    is_it_admin,
)


def get_user_incident_routes(user) -> QuerySet:
    queryset = (
        IncidentRoute.objects
        .select_related(
            "incident",
            "incident__camera",
            "incident__camera__location",
            "incident__acknowledged_by",
            "incident__acknowledged_unit",
            "security_unit",
            "acknowledged_by",
        )
    )
    if is_it_admin(user):
        return queryset
    unit_ids = get_user_security_units(user).values_list("pk", flat=True)
    return queryset.filter(security_unit_id__in=unit_ids, security_unit__active=True)


def get_user_incident_inbox(user) -> QuerySet:
    return (
        get_user_incident_routes(user)
        .filter(
            incident__status__in=[Incident.STATUS_OPEN, Incident.STATUS_ACKNOWLEDGED],
            status__in=[IncidentRoute.STATUS_PENDING, IncidentRoute.STATUS_ACKNOWLEDGED],
        )
        .order_by("-incident__detected_at", "routing_stage", "pk")
    )


def user_can_view_incident(user, incident: Incident) -> bool:
    if is_it_admin(user):
        return True
    return get_user_accessible_cameras(user).filter(pk=incident.camera_id).exists()


def user_can_ack_incident(user, incident: Incident) -> bool:
    if incident.status != Incident.STATUS_OPEN:
        return False
    if is_it_admin(user):
        return True
    return get_user_incident_routes(user).filter(
        incident=incident,
        status=IncidentRoute.STATUS_PENDING,
    ).exists()


def user_can_resolve_incident(user, incident: Incident) -> bool:
    if incident.status != Incident.STATUS_ACKNOWLEDGED:
        return False
    if is_it_admin(user):
        return True

    units = get_user_security_units(user)
    acknowledged_unit_access = bool(
        incident.acknowledged_unit_id
        and units.filter(pk=incident.acknowledged_unit_id).exists()
    )
    return acknowledged_unit_access or get_user_incident_routes(user).filter(
        incident=incident,
        security_unit__is_central=True,
        security_unit__active=True,
    ).exists()
