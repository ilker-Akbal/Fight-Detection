from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from adminx.models import SecurityUnit
from streams.models import Camera


class Incident(models.Model):
    TYPE_FIGHT = "FIGHT"
    TYPE_SPEED = "SPEED"
    TYPE_OTHER = "OTHER"
    TYPE_CHOICES = (
        (TYPE_FIGHT, "Fight"),
        (TYPE_SPEED, "Speed"),
        (TYPE_OTHER, "Other"),
    )

    STATUS_OPEN = "OPEN"
    STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
    STATUS_RESOLVED = "RESOLVED"
    STATUS_CHOICES = (
        (STATUS_OPEN, "Open"),
        (STATUS_ACKNOWLEDGED, "Acknowledged"),
        (STATUS_RESOLVED, "Resolved"),
    )

    ROUTING_PENDING = "PENDING"
    ROUTING_ROUTED = "ROUTED"
    ROUTING_UNROUTED = "UNROUTED"
    ROUTING_CHOICES = (
        (ROUTING_PENDING, "Pending"),
        (ROUTING_ROUTED, "Routed"),
        (ROUTING_UNROUTED, "Unrouted"),
    )

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_system = models.CharField(max_length=80, default="fight_runtime")
    run_id = models.CharField(max_length=100)
    external_incident_id = models.CharField(max_length=180)
    incident_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_FIGHT)
    camera = models.ForeignKey(
        Camera,
        on_delete=models.PROTECT,
        related_name="operational_incidents",
    )
    detected_at = models.DateTimeField()
    finalized_at = models.DateTimeField()
    ingested_at = models.DateTimeField(default=timezone.now)
    routing_started_at = models.DateTimeField(blank=True, null=True)
    routing_state = models.CharField(
        max_length=20,
        choices=ROUTING_CHOICES,
        default=ROUTING_PENDING,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    label = models.CharField(max_length=80, blank=True)
    decision_score = models.FloatField(default=0.0)
    max_score = models.FloatField(default=0.0)
    mean_score = models.FloatField(default=0.0)
    part_count = models.PositiveIntegerField(default=0)
    evidence_path = models.CharField(max_length=1000, blank=True)
    evidence_valid = models.BooleanField(default=False)
    ingest_error = models.CharField(max_length=500, blank=True)
    acknowledged_at = models.DateTimeField(blank=True, null=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="acknowledged_incidents",
        blank=True,
        null=True,
    )
    acknowledged_unit = models.ForeignKey(
        SecurityUnit,
        on_delete=models.PROTECT,
        related_name="acknowledged_incidents",
        blank=True,
        null=True,
    )
    resolved_at = models.DateTimeField(blank=True, null=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="resolved_incidents",
        blank=True,
        null=True,
    )
    resolution_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-detected_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_system", "run_id", "external_incident_id"],
                name="unique_incident_source_run_external",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "routing_state"], name="incident_status_route_idx"),
            models.Index(fields=["camera", "-detected_at"], name="incident_camera_time_idx"),
        ]

    def __str__(self):
        return f"{self.incident_type} {self.external_incident_id} ({self.status})"


class IncidentRoutingRule(models.Model):
    name = models.CharField(max_length=180)
    security_unit = models.ForeignKey(
        SecurityUnit,
        on_delete=models.PROTECT,
        related_name="incident_routing_rules",
    )
    incident_type = models.CharField(
        max_length=20,
        choices=Incident.TYPE_CHOICES,
        default=Incident.TYPE_FIGHT,
    )
    routing_stage = models.PositiveIntegerField(default=0)
    delay_sec = models.PositiveIntegerField(default=0)
    priority = models.IntegerField(default=100)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["incident_type", "routing_stage", "priority", "security_unit__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["security_unit", "incident_type", "routing_stage"],
                condition=Q(active=True),
                name="unique_active_unit_incident_stage",
            ),
        ]
        indexes = [
            models.Index(
                fields=["incident_type", "active", "routing_stage", "delay_sec"],
                name="routing_rule_due_idx",
            ),
        ]

    def __str__(self):
        return f"{self.name}: {self.incident_type} stage {self.routing_stage} +{self.delay_sec}s"


class IncidentRoute(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
    STATUS_SUPERSEDED = "SUPERSEDED"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_ACKNOWLEDGED, "Acknowledged"),
        (STATUS_SUPERSEDED, "Superseded"),
    )

    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="routes")
    security_unit = models.ForeignKey(
        SecurityUnit,
        on_delete=models.PROTECT,
        related_name="incident_routes",
    )
    routing_stage = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    routed_at = models.DateTimeField(default=timezone.now)
    acknowledged_at = models.DateTimeField(blank=True, null=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="acknowledged_incident_routes",
        blank=True,
        null=True,
    )
    routing_rule = models.ForeignKey(
        IncidentRoutingRule,
        on_delete=models.SET_NULL,
        related_name="routes",
        blank=True,
        null=True,
    )
    reason = models.CharField(max_length=180, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["incident", "routing_stage", "routed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["incident", "security_unit", "routing_stage"],
                name="unique_incident_unit_stage_route",
            ),
        ]
        indexes = [
            models.Index(fields=["security_unit", "status", "-routed_at"], name="route_unit_inbox_idx"),
        ]

    def __str__(self):
        return f"{self.incident} → {self.security_unit} / stage {self.routing_stage}"


class IncidentAuditEvent(models.Model):
    ACTION_INGESTED = "INGESTED"
    ACTION_ROUTED = "ROUTED"
    ACTION_ROUTING_FAILED = "ROUTING_FAILED"
    ACTION_ACKNOWLEDGED = "ACKNOWLEDGED"
    ACTION_ESCALATED = "ESCALATED"
    ACTION_RESOLVED = "RESOLVED"
    ACTION_CHOICES = (
        (ACTION_INGESTED, "Ingested"),
        (ACTION_ROUTED, "Routed"),
        (ACTION_ROUTING_FAILED, "Routing failed"),
        (ACTION_ACKNOWLEDGED, "Acknowledged"),
        (ACTION_ESCALATED, "Escalated"),
        (ACTION_RESOLVED, "Resolved"),
    )

    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="audit_events")
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="incident_audit_actions",
        blank=True,
        null=True,
    )
    security_unit = models.ForeignKey(
        SecurityUnit,
        on_delete=models.SET_NULL,
        related_name="incident_audit_events",
        blank=True,
        null=True,
    )
    routing_stage = models.PositiveIntegerField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["incident", "created_at"], name="audit_incident_time_idx")]

    def __str__(self):
        return f"{self.incident_id} {self.action}"


class IncidentIngestCursor(models.Model):
    source_identifier = models.CharField(max_length=500, unique=True)
    byte_offset = models.PositiveBigIntegerField(default=0)
    file_identity = models.CharField(max_length=180, blank=True)
    last_event_id = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.source_identifier}@{self.byte_offset}"


class IncidentIngestRecord(models.Model):
    STATUS_IMPORTED = "IMPORTED"
    STATUS_RETRYABLE = "RETRYABLE"
    STATUS_INVALID = "INVALID"
    STATUS_DUPLICATE = "DUPLICATE"
    STATUS_CHOICES = (
        (STATUS_IMPORTED, "Imported"),
        (STATUS_RETRYABLE, "Retryable"),
        (STATUS_INVALID, "Invalid"),
        (STATUS_DUPLICATE, "Duplicate"),
    )

    event_id = models.CharField(max_length=100, unique=True)
    source_identifier = models.CharField(max_length=500)
    byte_offset = models.PositiveBigIntegerField()
    raw_envelope = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    attempts = models.PositiveIntegerField(default=1)
    incident = models.ForeignKey(
        Incident,
        on_delete=models.SET_NULL,
        related_name="ingest_records",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["source_identifier", "byte_offset"], name="ingest_source_offset_idx")]

    def __str__(self):
        return f"{self.event_id}: {self.status}"
