from django.contrib import admin
from .models import Camera


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "camera_id",
        "location",
        "faculty",
        "source",
        "is_active",
        "created_at",
    )

    search_fields = (
        "name",
        "camera_id",
        "source",
        "location__name",
        "location__code",
        "faculty",
    )

    list_filter = (
        "is_active",
        "location__location_type",
        "created_at",
    )

    autocomplete_fields = ("location",)
    list_select_related = ("location",)
