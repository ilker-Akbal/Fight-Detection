from tools.transcode_incident_mp4s import incident_paths


def test_recursive_scan_only_selects_incident_directories(tmp_path):
    incident = tmp_path / "run-1" / "incidents" / "incident.mp4"
    segment = tmp_path / "run-1" / "temp_segments" / "segment.mp4"
    unrelated = tmp_path / "unrelated.mp4"
    for path in (incident, segment, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")

    assert list(incident_paths(tmp_path, recursive=True)) == [incident]


def test_incidents_directory_can_be_scanned_recursively(tmp_path):
    incidents = tmp_path / "incidents"
    nested = incidents / "archive" / "incident.mp4"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"video")

    assert list(incident_paths(incidents, recursive=True)) == [nested]
