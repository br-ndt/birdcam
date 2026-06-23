"""Per-camera rotation. It's a display concern only -- the backend records and streams in
native sensor orientation and the viewer applies the rotation -- so this just stores and
persists one of 0/90/180/270 and reports it over the API."""
import threading

from birdcam.config import ROTATION_FILE, DEFAULT_ROTATION

VALID_ROTATIONS = (0, 90, 180, 270)

_rotation_lock = threading.Lock()
_rotation = DEFAULT_ROTATION


def load_rotation():
    global _rotation
    try:
        val = int(ROTATION_FILE.read_text().strip())
        if val in VALID_ROTATIONS:
            _rotation = val
    except (OSError, ValueError):
        pass


def get_rotation():
    with _rotation_lock:
        return _rotation


def set_rotation(deg):
    """Set rotation to 0/90/180/270 (degrees clockwise). Persists across restarts."""
    global _rotation
    deg = int(deg) % 360
    if deg not in VALID_ROTATIONS:
        raise ValueError("rotation must be 0, 90, 180, or 270")
    with _rotation_lock:
        _rotation = deg
    try:
        ROTATION_FILE.write_text(str(deg))
    except OSError:
        pass
    return deg
