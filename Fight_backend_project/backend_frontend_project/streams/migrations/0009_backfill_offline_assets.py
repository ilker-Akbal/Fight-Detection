from pathlib import Path

from django.conf import settings
from django.db import migrations


def backfill(apps, schema_editor):
    Camera = apps.get_model("streams", "Camera")
    Asset = apps.get_model("streams", "OfflineAsset")
    Location = apps.get_model("adminx", "Location")
    for camera in Camera.objects.all().iterator():
        source = str(camera.source or "").strip()
        live = not camera.uploaded_video and bool(source) and (
            source.isdigit() or source.lower().startswith(("http://", "https://", "rtsp://", "rtsps://", "/dev/video")))
        Camera.objects.filter(pk=camera.pk).update(source_kind="LIVE" if live else "OFFLINE")
        if not live and (camera.uploaded_video or source):
            path = Path(settings.MEDIA_ROOT) / camera.uploaded_video.name if camera.uploaded_video else Path(source)
            if not path.is_absolute():
                path = Path(settings.REPO_ROOT) / path
            location_id = camera.location_id
            if not location_id and camera.faculty:
                location_id = Location.objects.filter(code=camera.faculty).values_list("pk", flat=True).first()
            Asset.objects.get_or_create(legacy_camera_id=camera.pk, defaults={
                "name": camera.name, "location_id": location_id, "file_path": str(path.resolve())})
    # No media moves, no Incident rewrites, no calibration changes, no implicit runs.


class Migration(migrations.Migration):
    dependencies = [("streams", "0008_offline_analysis_domain")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
