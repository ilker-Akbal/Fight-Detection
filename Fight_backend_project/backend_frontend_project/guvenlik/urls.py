from django.conf import settings
from django.conf.urls.static import static
from django.urls import path
from . import operator_views
from streams import offline_views

from .views import (
    index,
    status,
    stream,
    start_detection,
    stop_detection,
    events,
    events_stream,
    preview_image,
    incident_video,
    incident_ack,
    incident_resolve,
    incident_evidence,
)

app_name = "dashboard"

urlpatterns = [
    path("video-analyses/", offline_views.listing, name="offline_list"),
    path("video-analyses/new/", offline_views.upload, name="offline_upload"),
    path("video-analyses/<int:pk>/", offline_views.detail, name="offline_detail"),
    path("video-analyses/<int:pk>/submit/", offline_views.submit, name="offline_submit"),
    path("video-analyses/<int:pk>/video/", offline_views.playback, name="offline_playback"),
    path("video-analyses/run/<uuid:run_id>/cancel/", offline_views.cancel, name="offline_cancel"),
    path("video-analyses/result/<int:pk>/evidence/", offline_views.evidence, name="offline_evidence"),
    path("analytics/<str:action>/", operator_views.analytics_control, name="analytics_control"),
    path("history/", operator_views.history, name="history"),
    path("overview/", operator_views.overview, name="overview"),
    path("live-preview/<str:camera_id>/", operator_views.live_preview, name="live_preview"),
    path("live-previews/", operator_views.live_previews, name="live_previews"),
    path("incidents/<int:pk>/", operator_views.incident_detail, name="incident_detail"),
    path("", index, name="index"),
    path("status/", status, name="status"),
    path("events/", events, name="events"),
    path("events-stream/", events_stream, name="events_stream"),
    path("start-detection/", start_detection, name="start_detection"),
    path("stop-detection/", stop_detection, name="stop_detection"),
    path("stream/<str:camera_id>/", stream, name="stream"),
    path("preview/<str:camera_id>/", preview_image, name="preview_image"),
    path("incident-video/<str:run_name>/<path:clip_name>/", incident_video, name="incident_video"),
    path("incidents/<int:pk>/ack/", incident_ack, name="incident_ack"),
    path("incidents/<int:pk>/resolve/", incident_resolve, name="incident_resolve"),
    path("incidents/<int:pk>/evidence/", incident_evidence, name="incident_evidence"),
]

from streams.protected_media import scoped_media
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT, view=scoped_media)
