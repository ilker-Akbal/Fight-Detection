from django.urls import path

from . import views

app_name = "speed_detection"

urlpatterns = [
    path("", views.index, name="index"),

    path("start/", views.start_speed_detection, name="start"),
    path("stop/", views.stop_speed_detection, name="stop"),
    path("status/", views.speed_status, name="status"),
    path("events/", views.speed_events, name="events"),

    path("calibration-frame/<str:camera_id>/", views.speed_calibration_frame, name="calibration_frame"),
    path("save-calibration/<str:camera_id>/", views.save_speed_calibration, name="save_calibration"),

    path("snapshot/<str:run_name>/<str:file_name>/", views.speed_snapshot, name="snapshot"),
    path("preview/<str:run_name>/<str:camera_id>/", views.speed_preview, name="preview"),

    # Üst kamera kartındaki temiz ham canlı akış
    path("camera-stream/<str:camera_id>/", views.speed_camera_stream, name="camera_stream"),

    path("clip/<str:run_name>/<str:file_name>/", views.speed_clip, name="clip"),
]