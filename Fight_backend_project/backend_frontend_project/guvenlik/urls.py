from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

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
)

app_name = "dashboard"

urlpatterns = [
    path("", index, name="index"),
    path("status/", status, name="status"),
    path("events/", events, name="events"),
    path("events-stream/", events_stream, name="events_stream"),
    path("start-detection/", start_detection, name="start_detection"),
    path("stop-detection/", stop_detection, name="stop_detection"),
    path("stream/<str:camera_id>/", stream, name="stream"),
    path("preview/<str:camera_id>/", preview_image, name="preview_image"),
    path("incident-video/<str:run_name>/<path:clip_name>/", incident_video, name="incident_video"),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)