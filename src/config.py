"""Configuration + on-disk paths, shared by every entrypoint. Loading happens at import:
the capture dirs are created, stale temporaries are swept, and the TOML config + token are
resolved (a missing token is fatal). Keep this import-light -- no cv2, flask, or picamera2 --
so the leanest entrypoint pulls only what it needs."""
import logging
import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # py < 3.11
    import tomli as tomllib

from birdcam import __version__ as VERSION  # noqa: F401  (re-exported for the API)

# quiet down Flask's request logging (harmless if Flask isn't loaded)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

CAPTURE_DIR = Path("captures")
THUMB_DIR = CAPTURE_DIR / ".thumbnails"
TMP_DIR = CAPTURE_DIR / ".tmp"
ROTATION_FILE = CAPTURE_DIR / ".rotation"
RECORDING_FLAG_FILE = CAPTURE_DIR / ".recording_enabled"
DEFAULT_ROTATION = 0     # rotation is display-side; this is just the stored default

CONFIG_PATHS = [
    Path("/etc/birdcam/config.toml"),
    Path.home() / ".config/birdcam/config.toml",
    Path("birdcam.toml"),
]


def ensure_dirs():
    """Create the capture dirs and clear video/audio/clip temporaries orphaned by a previous
    crash or kill (including legacy .part files earlier versions wrote alongside clips)."""
    CAPTURE_DIR.mkdir(exist_ok=True)
    THUMB_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    for stale in list(TMP_DIR.iterdir()) + list(CAPTURE_DIR.glob("*.part")):
        try:
            stale.unlink()
        except OSError:
            pass


def load_config():
    """Load config from the first config file found, with env-var override for the token."""
    cfg = {"name": os.uname().nodename, "token": None}
    for path in CONFIG_PATHS:
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            cfg.update(data)
            print(f"loaded config from {path}")
            break
    env_token = os.environ.get("BIRDCAM_TOKEN")
    if env_token:
        cfg["token"] = env_token
    if not cfg["token"]:
        raise SystemExit(
            "No auth token configured. Set BIRDCAM_TOKEN env var or "
            "add `token = \"...\"` to /etc/birdcam/config.toml"
        )
    return cfg


ensure_dirs()
CONFIG = load_config()

# --- capture / detection (shared) ---
LOW_RES             = tuple(CONFIG.get("low_res", (640, 480)))
HIGH_RES            = tuple(CONFIG.get("high_res", (1920, 1080)))
FPS                 = CONFIG.get("fps", 30)
MOTION_THRESHOLD    = CONFIG.get("motion_threshold", 5000)
QUIET_SECONDS       = CONFIG.get("quiet_seconds", 3)
MAX_CLIP_SECONDS    = CONFIG.get("max_clip_seconds", 120)
RECORD_COOLDOWN     = CONFIG.get("record_cooldown", 5)

# --- web + housekeeping (shared) ---
PORT                = CONFIG.get("port", 5000)
STREAM_FPS          = CONFIG.get("stream_fps", 10)
STREAM_QUALITY      = CONFIG.get("stream_quality", 80)
THUMB_WIDTH         = CONFIG.get("thumb_width", 320)
HEARTBEAT_TIMEOUT   = CONFIG.get("heartbeat_timeout", 30)
MIN_FREE_MB         = CONFIG.get("min_free_mb", 500)
RETENTION_TARGET_MB = CONFIG.get("retention_target_mb", 1000)

# --- standalone (camera) only ---
MIC_DEVICE          = CONFIG.get("mic_device", "plughw:2,0")
LENS_POSITION       = CONFIG.get("lens_position", 1.4)
VIDEO_BITRATE       = CONFIG.get("video_bitrate", "4M")   # H264Encoder bitrate; "4M"/"4000k"/int
RECORD_AUDIO        = CONFIG.get("record_audio", True)    # false -> video-only (skips the mic probe)

# --- recorder (RTSP ingest) only ---
RTSP_MAIN   = CONFIG.get("rtsp_main", "")     # node main stream, e.g. "rtsp://sarkos:8554/cam"
RTSP_DETECT = CONFIG.get("rtsp_detect", "")   # low-res detect substream; falls back to RTSP_MAIN if empty
