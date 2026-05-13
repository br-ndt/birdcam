"""
Bird feeder cam: motion-triggered A/V capture + live MJPEG stream + web viewer.
All in one process so we share a single camera instance.
"""
import logging
import os
import shutil
import subprocess
import threading
try:
    import tomllib
except ImportError:
    import tomli as tomllib

from datetime import datetime
from functools import wraps
from pathlib import Path
from time import sleep, time

import cv2
from flask import Flask, Response, abort, jsonify, request, send_from_directory
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

# quiet down Flask's request logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# --- config ---
CAPTURE_DIR = Path("captures")
THUMB_DIR = CAPTURE_DIR / ".thumbnails"
LOW_RES = (640, 480)
HIGH_RES = (1920, 1080)
MIC_DEVICE = "plughw:2,0"
MOTION_THRESHOLD = 5000
QUIET_SECONDS = 3
MAX_CLIP_SECONDS = 120
LOOP_DELAY = 0.05
PORT = 5000
STREAM_FPS = 10
STREAM_QUALITY = 80
THUMB_WIDTH = 320
VERSION = "0.2.0"

CONFIG_PATHS = [
    Path("/etc/birdcam/config.toml"),
    Path.home() / ".config/birdcam/config.toml",
    Path("birdcam.toml"),
]

CAPTURE_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)


def load_config():
    """Load config from the first config file we find, with env var override for token."""
    cfg = {"name": os.uname().nodename, "token": None}
    for path in CONFIG_PATHS:
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            cfg.update(data)
            print(f"loaded config from {path}")
            break
    # env var wins so systemd unit can inject it without a file on disk
    env_token = os.environ.get("BIRDCAM_TOKEN")
    if env_token:
        cfg["token"] = env_token
    if not cfg["token"]:
        raise SystemExit(
            "No auth token configured. Set BIRDCAM_TOKEN env var or "
            "add `token = \"...\"` to /etc/birdcam/config.toml"
        )
    return cfg


CONFIG = load_config()
STARTED_AT = time()

# --- camera setup (single shared instance) ---
picam2 = Picamera2()
config = picam2.create_video_configuration(
    main={"size": HIGH_RES, "format": "RGB888"},
    lores={"size": LOW_RES, "format": "YUV420"},
)
picam2.configure(config)
picam2.start()
sleep(2)

encoder = H264Encoder(bitrate=4_000_000)

# --- shared state for the live stream + status ---
latest_jpeg = None
latest_jpeg_lock = threading.Lock()
new_frame_event = threading.Event()

state_lock = threading.Lock()
state = {
    "recording": False,
    "recording_filename": None,
    "recording_started_at": None,
    "last_motion_at": None,
}


def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)


def get_state():
    with state_lock:
        return dict(state)


def merge_async(video_path, audio_path, final_path, on_done=None):
    def _do():
        result = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", video_path, "-i", audio_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac",
            final_path,
        ])
        if result.returncode == 0:
            print(f"  merged -> {final_path}")
            if on_done:
                try:
                    on_done(final_path)
                except Exception as e:
                    print(f"  on_done failed: {e}")
        else:
            print(f"  merge failed for {final_path}")
    threading.Thread(target=_do, daemon=True).start()


def generate_thumbnail(clip_path, thumb_path):
    """Pull the first frame of a clip and save as a small JPEG. Returns True on success."""
    result = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(clip_path),
        "-vf", f"scale={THUMB_WIDTH}:-1",
        "-frames:v", "1",
        "-q:v", "5",
        str(thumb_path),
    ])
    return result.returncode == 0 and thumb_path.exists()


def detect_motion(prev_frame, gray):
    blurred = cv2.GaussianBlur(gray, (21, 21), 0)
    if prev_frame is None:
        return 0, blurred
    diff = cv2.absdiff(prev_frame, blurred)
    thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
    return cv2.countNonZero(thresh), blurred


def update_stream_jpeg(yuv):
    """Convert lores YUV frame to JPEG and stash it for the stream endpoint."""
    global latest_jpeg
    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
    if ok:
        with latest_jpeg_lock:
            latest_jpeg = buf.tobytes()
        new_frame_event.set()


def motion_loop():
    """Runs in a background thread. Owns the camera lores reads."""
    prev_frame = None
    recording = False
    record_started_at = 0.0
    last_motion_at = 0.0
    last_stream_update = 0.0
    video_path = audio_path = final_path = None
    audio_proc = None

    print("Motion loop started.")
    while True:
        yuv = picam2.capture_array("lores")
        gray = yuv[:LOW_RES[1], :LOW_RES[0]]
        motion_pixels, prev_frame = detect_motion(prev_frame, gray)
        motion = motion_pixels > MOTION_THRESHOLD
        now = time()

        if motion:
            set_state(last_motion_at=now)

        # update stream JPEG at STREAM_FPS
        if now - last_stream_update > 1.0 / STREAM_FPS:
            update_stream_jpeg(yuv)
            last_stream_update = now

        if recording:
            if motion:
                last_motion_at = now
            quiet_for = now - last_motion_at
            recorded_for = now - record_started_at
            if quiet_for >= QUIET_SECONDS or recorded_for >= MAX_CLIP_SECONDS:
                picam2.stop_encoder()
                if audio_proc and audio_proc.poll() is None:
                    audio_proc.terminate()
                    audio_proc.wait(timeout=2)

                final_name = Path(final_path).name

                def on_merged(path):
                    # generate thumbnail eagerly so first request is fast
                    thumb = THUMB_DIR / (Path(path).stem + ".jpg")
                    generate_thumbnail(path, thumb)

                merge_async(video_path, audio_path, final_path, on_done=on_merged)
                reason = "max length" if recorded_for >= MAX_CLIP_SECONDS else "quiet"
                print(f"stopped ({reason}, {recorded_for:.1f}s)")
                recording = False
                set_state(
                    recording=False,
                    recording_filename=None,
                    recording_started_at=None,
                )
                prev_frame = None
        else:
            if motion:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base = f"{CAPTURE_DIR}/clip_{timestamp}"
                video_path = f"{base}.video.mp4"
                audio_path = f"{base}.audio.wav"
                final_path = f"{base}.mp4"
                audio_proc = subprocess.Popen([
                    "arecord", "-D", MIC_DEVICE, "-f", "cd", "-q", audio_path,
                ], stderr=subprocess.DEVNULL)
                output = FfmpegOutput(video_path)
                picam2.start_encoder(encoder, output)
                record_started_at = now
                last_motion_at = now
                recording = True
                set_state(
                    recording=True,
                    recording_filename=f"clip_{timestamp}.mp4",
                    recording_started_at=now,
                )
                print(f"motion: {motion_pixels} pixels, recording -> clip_{timestamp}.mp4")

        sleep(LOOP_DELAY)


# --- Flask app ---
app = Flask(__name__)


def is_valid_clip_name(filename):
    """Reject anything that isn't a final merged clip, including path traversal."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    if not filename.startswith("clip_") or not filename.endswith(".mp4"):
        return False
    # reject intermediates: check this BEFORE the .mp4 suffix check would have passed
    if filename.endswith(".video.mp4"):
        return False
    return True


def require_token(f):
    """Decorator that enforces bearer-token auth. Accepts header or ?token= query param."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            # MJPEG in an <img> tag can't send custom headers, so allow query string
            # as a fallback. Trusted-LAN tradeoff, documented in the readme.
            token = request.args.get("token")
        if not token or token != CONFIG["token"]:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def list_clips(page=1, per_page=10):
    """Return (clips_for_page, total_count) sorted newest first."""
    clips = []
    for f in CAPTURE_DIR.glob("clip_*.mp4"):
        if f.name.endswith(".video.mp4"):
            continue
        try:
            stem = f.stem.replace("clip_", "")
            dt = datetime.strptime(stem, "%Y%m%d_%H%M%S")
            size_mb = f.stat().st_size / (1024 * 1024)
            clips.append((f.name, dt, size_mb))
        except ValueError:
            continue
    clips.sort(key=lambda x: x[1], reverse=True)
    total = len(clips)
    start = (page - 1) * per_page
    end = start + per_page
    return clips[start:end], total


@app.route("/health")
def health():
    """Unauthenticated liveness probe. No sensitive info."""
    return jsonify({"ok": True, "version": VERSION})


@app.route("/api/status")
@require_token
def api_status():
    """Aggregator-friendly status snapshot. Cheap; safe to poll every ~10s."""
    s = get_state()
    # disk usage of the partition holding captures
    du = shutil.disk_usage(CAPTURE_DIR)
    # clip count + total bytes (one stat() per file, fine for a few hundred clips)
    clip_count = 0
    clip_bytes = 0
    for f in CAPTURE_DIR.glob("clip_*.mp4"):
        if f.name.endswith(".video.mp4"):
            continue
        clip_count += 1
        try:
            clip_bytes += f.stat().st_size
        except OSError:
            pass

    return jsonify({
        "name": CONFIG["name"],
        "version": VERSION,
        "uptime_seconds": int(time() - STARTED_AT),
        "recording": s["recording"],
        "recording_filename": s["recording_filename"],
        "recording_started_at": s["recording_started_at"],
        "last_motion_at": s["last_motion_at"],
        "resolution": {"main": list(HIGH_RES), "lores": list(LOW_RES)},
        "stream_fps": STREAM_FPS,
        "clip_count": clip_count,
        "clip_bytes": clip_bytes,
        "disk_total_bytes": du.total,
        "disk_free_bytes": du.free,
    })


@app.route("/api/clips")
@require_token
def api_clips():
    """Return current clips list as JSON for polling."""
    page = max(1, int(request.args.get("page", 1)))
    per_page = 20
    clips, total = list_clips(page=page, per_page=per_page)
    return jsonify({
        "total": total,
        "page": page,
        "total_pages": (total + per_page - 1) // per_page,
        "clips": [
            {
                "name": name,
                "timestamp": dt.isoformat(),
                "display_time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "size_mb": round(size, 1),
            }
            for name, dt, size in clips
        ],
    })


@app.route("/clips/<filename>")
@require_token
def serve_clip(filename):
    if not is_valid_clip_name(filename):
        abort(404)
    return send_from_directory(CAPTURE_DIR.absolute(), filename)


@app.route("/api/clips/<filename>/thumbnail")
@require_token
def api_clip_thumbnail(filename):
    """Lazy-generated thumbnail. Cached to disk after first request."""
    if not is_valid_clip_name(filename):
        abort(404)
    clip_path = CAPTURE_DIR / filename
    if not clip_path.exists():
        abort(404)
    thumb_name = clip_path.stem + ".jpg"
    thumb_path = THUMB_DIR / thumb_name
    if not thumb_path.exists():
        if not generate_thumbnail(clip_path, thumb_path):
            abort(500)
    return send_from_directory(THUMB_DIR.absolute(), thumb_name)


@app.route("/api/clips/<filename>", methods=["DELETE"])
@require_token
def api_delete_clip(filename):
    """Delete a clip and its intermediate files + thumbnail."""
    if not is_valid_clip_name(filename):
        return jsonify({"error": "invalid filename"}), 400

    target = CAPTURE_DIR / filename
    if not target.exists():
        return jsonify({"error": "not found"}), 404

    base = filename[:-4]  # strip .mp4
    deleted = []
    for suffix in [".mp4", ".video.mp4", ".audio.wav"]:
        f = CAPTURE_DIR / f"{base}{suffix}"
        if f.exists():
            f.unlink()
            deleted.append(f.name)
    # nuke thumbnail too
    thumb = THUMB_DIR / f"{base}.jpg"
    if thumb.exists():
        thumb.unlink()
        deleted.append(f".thumbnails/{thumb.name}")

    print(f"deleted: {deleted}")
    return jsonify({"deleted": deleted})


def mjpeg_generator():
    """Yields multipart MJPEG frames as bytes."""
    while True:
        new_frame_event.wait(timeout=2)
        new_frame_event.clear()
        with latest_jpeg_lock:
            frame = latest_jpeg
        if frame is None:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")


@app.route("/stream.mjpg")
@require_token
def stream():
    return Response(
        mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


if __name__ == "__main__":
    t = threading.Thread(target=motion_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)