from django.contrib import admin

from incidents.models import (
    Incident,
    IncidentAuditEvent,
    IncidentIngestCursor,
    IncidentIngestRecord,
    IncidentRoute,
    IncidentRoutingRule,
)


class IncidentRouteInline(admin.TabularInline):
    model = IncidentRoute
    extra = 0
    readonly_fields = ("routed_at", "acknowledged_at", "acknowledged_by")


class IncidentAuditInline(admin.TabularInline):
    model = IncidentAuditEvent
    extra = 0
    can_delete = False
    readonly_fields = (
        "action",
        "actor",
        "security_unit",
        "routing_stage",
        "created_at",
        "metadata",
    )


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = (
        "external_incident_id",
        "incident_type",
        "camera",
        "status",
        "routing_state",
        "detected_at",
        "acknowledged_by",
    )
    list_filter = ("incident_type", "status", "routing_state", "evidence_valid")
    search_fields = ("event_id", "run_id", "external_incident_id", "camera__camera_id")
    list_select_related = ("camera", "acknowledged_by", "acknowledged_unit")
    readonly_fields = (
        "event_id",
        "source_system",
        "run_id",
        "external_incident_id",
        "camera",
        "detected_at",
        "finalized_at",
        "ingested_at",
        "evidence_path",
        "evidence_valid",
        "ingest_error",
        "created_at",
        "updated_at",
    )
    inlines = (IncidentRouteInline, IncidentAuditInline)


@admin.register(IncidentRoutingRule)
class IncidentRoutingRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "security_unit",
        "incident_type",
        "routing_stage",
        "delay_sec",
        "priority",
        "active",
    )
    list_filter = ("active", "incident_type", "routing_stage")
    search_fields = ("name", "security_unit__name", "security_unit__code")
    autocomplete_fields = ("security_unit",)
    list_select_related = ("security_unit",)


@admin.register(IncidentRoute)
class IncidentRouteAdmin(admin.ModelAdmin):
    list_display = ("incident", "security_unit", "routing_stage", "status", "routed_at")
    list_filter = ("status", "routing_stage", "security_unit")
    search_fields = ("incident__external_incident_id", "security_unit__name")
    list_select_related = ("incident", "security_unit", "routing_rule")
    readonly_fields = ("created_at", "updated_at")


@admin.register(IncidentAuditEvent)
class IncidentAuditEventAdmin(admin.ModelAdmin):
    list_display = ("incident", "action", "security_unit", "actor", "routing_stage", "created_at")
    list_filter = ("action", "security_unit")
    search_fields = ("incident__external_incident_id", "actor__username")
    list_select_related = ("incident", "security_unit", "actor")
    readonly_fields = [field.name for field in IncidentAuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IncidentIngestCursor)
class IncidentIngestCursorAdmin(admin.ModelAdmin):
    list_display = ("source_identifier", "byte_offset", "last_event_id", "updated_at")
    readonly_fields = ("source_identifier", "byte_offset", "file_identity", "last_event_id", "updated_at")


@admin.register(IncidentIngestRecord)
class IncidentIngestRecordAdmin(admin.ModelAdmin):
    list_display = ("event_id", "status", "error_code", "incident", "attempts", "updated_at")
    list_filter = ("status", "error_code")
    search_fields = ("event_id", "incident__external_incident_id", "error_message")
    list_select_related = ("incident",)
    readonly_fields = [field.name for field in IncidentIngestRecord._meta.fields]
