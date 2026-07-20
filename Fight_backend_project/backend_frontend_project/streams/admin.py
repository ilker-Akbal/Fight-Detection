from django.contrib import admin
from .models import Camera


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "camera_id",
        "source",
        "is_active",
        "created_at",
    )

    search_fields = (
        "name",
        "camera_id",
        "source",
    )

    list_filter = (
        "is_active",
        "created_at",
    )