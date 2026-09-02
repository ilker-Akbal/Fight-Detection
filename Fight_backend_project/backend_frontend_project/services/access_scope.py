"""Central camera authorization and physical security scope rules.

All server-side camera access should enter through this module.  The service is
database-backed and intentionally does not cache authorization decisions so that
deactivating an assignment, unit, coverage, or location takes effect immediately.
"""

from __future__ import annotations

from collections import defaultdict

from django.db.models import Q, QuerySet

from adminx.models import (
    Location,
    SecurityUnit,
    SecurityUnitCoverage,
)
from streams.models import Camera


def _profile_for_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    try:
        return user.profile
    except Exception:
        return None


def is_it_admin(user) -> bool:
    """Return whether the user has the existing project-wide IT admin bypass."""

    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True

    profile = _profile_for_user(user)
    return bool(
        profile
        and profile.status == "approved"
        and profile.role == "admin"
    )


def get_user_security_units(user) -> QuerySet:
    """Active units reached through an active user assignment."""

    if not user or not getattr(user, "is_authenticated", False):
        return SecurityUnit.objects.none()
    if is_it_admin(user):
        return SecurityUnit.objects.filter(active=True).select_related("location")

    profile = _profile_for_user(user)
    if not profile or profile.status != "approved":
        return SecurityUnit.objects.none()

    return (
        SecurityUnit.objects
        .filter(
            active=True,
            user_assignments__user=user,
            user_assignments__active=True,
        )
        .select_related("location")
        .distinct()
    )


def _effectively_active_location_ids(rows):
    parent_by_id = {location_id: parent_id for location_id, parent_id, _ in rows}
    active_by_id = {location_id: active for location_id, _, active in rows}
    memo = {}

    def is_effectively_active(location_id, visiting=None):
        if location_id in memo:
            return memo[location_id]
        if not active_by_id.get(location_id, False):
            memo[location_id] = False
            return False

        visiting = set(visiting or ())
        if location_id in visiting:
            memo[location_id] = False
            return False
        visiting.add(location_id)

        parent_id = parent_by_id.get(location_id)
        result = parent_id is None or is_effectively_active(parent_id, visiting)
        memo[location_id] = result
        return result

    return {
        location_id
        for location_id in parent_by_id
        if is_effectively_active(location_id)
    }


def _covered_location_ids(user) -> set[int]:
    if is_it_admin(user):
        return set(Location.objects.values_list("pk", flat=True))

    unit_ids = get_user_security_units(user).values_list("pk", flat=True)
    coverages = list(
        SecurityUnitCoverage.objects
        .filter(active=True, security_unit_id__in=unit_ids)
        .values_list("location_id", "include_descendants")
    )
    if not coverages:
        return set()

    rows = list(Location.objects.values_list("pk", "parent_id", "active"))
    effectively_active = _effectively_active_location_ids(rows)
    children_by_parent = defaultdict(list)
    for location_id, parent_id, _ in rows:
        children_by_parent[parent_id].append(location_id)

    covered = set()
    for root_id, include_descendants in coverages:
        if root_id not in effectively_active:
            continue
        covered.add(root_id)
        if not include_descendants:
            continue

        stack = [root_id]
        while stack:
            parent_id = stack.pop()
            for child_id in children_by_parent.get(parent_id, ()):
                if child_id in covered or child_id not in effectively_active:
                    continue
                covered.add(child_id)
                stack.append(child_id)

    return covered


def get_user_location_scope(user) -> QuerySet:
    """Locations covered by the user's active security assignments."""

    ids = _covered_location_ids(user)
    return Location.objects.filter(pk__in=ids).select_related("parent")


def _legacy_faculty_codes(user, location_ids) -> set[str]:
    codes = set(
        Location.objects
        .filter(pk__in=location_ids)
        .exclude(code="")
        .values_list("code", flat=True)
    )
    profile = _profile_for_user(user)
    if profile and profile.status == "approved" and profile.faculty:
        codes.add(str(profile.faculty))
    return codes


def get_user_accessible_cameras(
    user,
    *,
    active_only: bool = False,
    fight_only: bool = False,
    speed_only: bool = False,
) -> QuerySet:
    """Return the only Camera queryset views should expose to ``user``.

    Cameras without the new FK retain a temporary read fallback through the old
    ``faculty`` code.  Cameras with a location never fall back: their physical
    tree and active ancestors are authoritative.
    """

    queryset = Camera.objects.select_related("location")
    if active_only:
        queryset = queryset.filter(is_active=True)
    if fight_only:
        queryset = queryset.filter(use_fight_detection=True)
    if speed_only:
        queryset = queryset.filter(use_speed_detection=True)

    if is_it_admin(user):
        return queryset.order_by("-created_at")

    profile = _profile_for_user(user)
    if not profile or profile.status != "approved":
        return queryset.none()

    location_ids = _covered_location_ids(user)
    legacy_codes = _legacy_faculty_codes(user, location_ids)
    scope_filter = Q(location_id__in=location_ids)
    if legacy_codes:
        scope_filter |= Q(location__isnull=True, faculty__in=legacy_codes)

    return queryset.filter(scope_filter).distinct().order_by("-created_at")


def user_can_access_camera(user, camera) -> bool:
    """Check access to a Camera instance, numeric PK, or camera_id string."""

    queryset = get_user_accessible_cameras(user)
    if isinstance(camera, Camera):
        if camera.pk is None:
            return False
        return queryset.filter(pk=camera.pk).exists()
    if isinstance(camera, int):
        return queryset.filter(pk=camera).exists()
    return queryset.filter(camera_id=str(camera or "")).exists()


def user_can_manage_camera(user, camera=None) -> bool:
    """Camera create/update/delete and runtime controls remain IT-admin only."""

    if not is_it_admin(user):
        return False
    if camera is None:
        return True
    if isinstance(camera, Camera):
        return camera.pk is not None
    if isinstance(camera, int):
        return Camera.objects.filter(pk=camera).exists()
    return Camera.objects.filter(camera_id=str(camera or "")).exists()

