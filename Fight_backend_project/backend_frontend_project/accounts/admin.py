from django.contrib import admin
from .models import UserProfile, LoginActivity


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "faculty",
        "role",
        "status",
    )
    list_filter = (
        "faculty",
        "role",
        "status",
    )
    search_fields = (
        "user__username",
        "user__email",
        "faculty",
    )


@admin.register(LoginActivity)
class LoginActivityAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "role_at_login",
        "ip_address",
        "login_at",
    )
    list_filter = (
        "role_at_login",
        "login_at",
    )
    search_fields = (
        "user__username",
        "user__email",
        "ip_address",
    )