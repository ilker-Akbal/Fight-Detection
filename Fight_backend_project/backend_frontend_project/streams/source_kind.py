"""Product source classification; never opens a source or probes the network."""
def is_live_source(source, uploaded_video=None):
    value = str(source or "").strip().lower()
    return not uploaded_video and bool(value) and (
        value.isdigit() or value.startswith(("rtsp://", "rtsps://", "http://", "https://", "/dev/video")))
