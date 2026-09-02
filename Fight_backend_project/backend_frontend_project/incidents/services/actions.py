from __future__ import annotations

import time

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.utils import timezone

from incidents.models import Incident, IncidentAuditEvent, IncidentRoute
from services.access_scope import is_it_admin
from services.incident_access import (
    get_user_incident_routes,
    user_can_resolve_incident,
)


def _user_route_for_incident(user, incident, security_unit_id=None):
    routes = get_user_incident_routes(user).filter(incident=incident).order_by(
        "routing_stage", "routed_at", "pk"
    )
    if security_unit_id is not None:
        routes = routes.filter(security_unit_id=security_unit_id)
    return routes.first()


@transaction.atomic
def _acknowledge_once(user, incident_id, security_unit_id=None) -> dict:
    incident = Incident.objects.select_for_update().get(pk=incident_id)
    route = _user_route_for_incident(user, incident, security_unit_id)
    if route is None and not is_it_admin(user):
        raise PermissionDenied("Bu olay kullanıcının güvenlik birimine route edilmemiş.")

    if incident.status != Incident.STATUS_OPEN:
        return {"result": "already_acknowledged", "incident": incident, "route": route}

    now = timezone.now()
    unit_id = route.security_unit_id if route is not None else None
    updated = Incident.objects.filter(pk=incident.pk, status=Incident.STATUS_OPEN).update(
        status=Incident.STATUS_ACKNOWLEDGED,
        acknowledged_at=now,
        acknowledged_by=user,
        acknowledged_unit_id=unit_id,
        updated_at=now,
    )
    if updated == 0:
        incident.refresh_from_db()
        return {"result": "already_acknowledged", "incident": incident, "route": route}

    if route is not None:
        IncidentRoute.objects.filter(pk=route.pk).update(
            status=IncidentRoute.STATUS_ACKNOWLEDGED,
            acknowledged_at=now,
            acknowledged_by=user,
            updated_at=now,
        )
    IncidentAuditEvent.objects.create(
        incident=incident,
        action=IncidentAuditEvent.ACTION_ACKNOWLEDGED,
        actor=user,
        security_unit_id=unit_id,
        routing_stage=route.routing_stage if route is not None else None,
        metadata={"route_id": route.pk if route is not None else None},
    )
    incident.refresh_from_db()
    return {"result": "acknowledged", "incident": incident, "route": route}


def acknowledge_incident(user, incident, security_unit_id=None) -> dict:
    for attempt in range(5):
        try:
            return _acknowledge_once(user, incident.pk, security_unit_id)
        except OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


@transaction.atomic
def resolve_incident(user, incident, resolution_note: str) -> dict:
    locked = Incident.objects.select_for_update().get(pk=incident.pk)
    if locked.status == Incident.STATUS_RESOLVED:
        return {"result": "already_resolved", "incident": locked}
    if not user_can_resolve_incident(user, locked):
        raise PermissionDenied("Bu olayı çözümleme yetkiniz yok.")

    note = str(resolution_note or "").strip()
    if not note:
        raise ValidationError("Çözüm notu zorunludur.")
    if len(note) > 4000:
        raise ValidationError("Çözüm notu en fazla 4000 karakter olabilir.")

    now = timezone.now()
    updated = Incident.objects.filter(
        pk=locked.pk,
        status=Incident.STATUS_ACKNOWLEDGED,
    ).update(
        status=Incident.STATUS_RESOLVED,
        resolved_at=now,
        resolved_by=user,
        resolution_note=note,
        updated_at=now,
    )
    if updated == 0:
        locked.refresh_from_db()
        return {"result": "already_resolved", "incident": locked}

    IncidentAuditEvent.objects.create(
        incident=locked,
        action=IncidentAuditEvent.ACTION_RESOLVED,
        actor=user,
        security_unit=locked.acknowledged_unit,
        metadata={"resolution_note_length": len(note)},
    )
    locked.refresh_from_db()
    return {"result": "resolved", "incident": locked}
