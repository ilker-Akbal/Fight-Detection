from django.contrib import admin

from .models import (
    FacultyLocation,
    Location,
    SecurityUnit,
    SecurityUnitCoverage,
    UserSecurityAssignment,
)


@admin.register(FacultyLocation)
class FacultyLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "location_type", "parent", "active", "updated_at")
    list_filter = ("active", "location_type")
    search_fields = ("name", "code", "description")
    autocomplete_fields = ("parent",)
    list_select_related = ("parent",)


class SecurityUnitCoverageInline(admin.TabularInline):
    model = SecurityUnitCoverage
    extra = 0
    autocomplete_fields = ("location",)


class UserSecurityAssignmentInline(admin.TabularInline):
    model = UserSecurityAssignment
    extra = 0
    autocomplete_fields = ("user",)


@admin.register(SecurityUnit)
class SecurityUnitAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "unit_type", "location", "active", "is_central")
    list_filter = ("active", "is_central", "unit_type")
    search_fields = ("name", "code")
    autocomplete_fields = ("location",)
    list_select_related = ("location",)
    inlines = (SecurityUnitCoverageInline, UserSecurityAssignmentInline)


@admin.register(SecurityUnitCoverage)
class SecurityUnitCoverageAdmin(admin.ModelAdmin):
    list_display = ("security_unit", "location", "include_descendants", "active", "updated_at")
    list_filter = ("active", "include_descendants")
    search_fields = ("security_unit__name", "security_unit__code", "location__name", "location__code")
    autocomplete_fields = ("security_unit", "location")
    list_select_related = ("security_unit", "location")


@admin.register(UserSecurityAssignment)
class UserSecurityAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "security_unit", "role_in_unit", "active", "created_at")
    list_filter = ("active", "security_unit")
    search_fields = ("user__username", "user__email", "security_unit__name", "security_unit__code")
    autocomplete_fields = ("user", "security_unit")
    list_select_related = ("user", "security_unit")
