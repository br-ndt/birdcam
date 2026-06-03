"""
Bird feeder cam: motion-triggered A/V capture + live MJPEG stream + web viewer.

All in one process so we share a single camera instance.

Rotation is handled in software.
"""
import logging
import os
import queue
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
from libcamera import controls
from picamera2 import Picamera2

# quiet down Flask's request logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# --- config ---
CAPTURE_DIR = Path("captures")
THUMB_DIR = CAPTURE_DIR / ".thumbnails"
ROTATION_FILE = CAPTURE_DIR / ".rotation"
LOW_RES = (640, 480)
HIGH_RES = (1920, 1080)
FPS = 30
MIC_DEVICE = "plughw:2,0"
MOTION_THRESHOLD = 5000
QUIET_SECONDS = 3
MAX_CLIP_SECONDS = 120
PORT = 5000
STREAM_FPS = 10
STREAM_QUALITY = 80
THUMB_WIDTH = 320
LENS_POSITION = 1.4        # Module 3 manual focus. dioptres = 1 / metres (1.4 ~= 0.7 m). 0.0 = infinity.
DEFAULT_ROTATION = 270     # degrees clockwise; 270 == 90 CCW, which corrects a camera mounted 90 clockwise.
FRAME_QUEUE_MAX = FPS * 2  # ~2 s of frames buffered to the encoder before we start dropping rather than stall.
VERSION = "0.3.0"

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


# --- rotation: single source of truth, runtime-settable, persisted across restarts ---
ROTATION_TO_CV2 = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}
_rotation_lock = threading.Lock()
_rotation = DEFAULT_ROTATION


def load_rotation():
    global _rotation
    try:
        val = int(ROTATION_FILE.read_text().strip())
        if val in ROTATION_TO_CV2:
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
    if deg not in ROTATION_TO_CV2:
        raise ValueError("rotation must be 0, 90, 180, or 270")
    with _rotation_lock:
        _rotation = deg
    try:
        ROTATION_FILE.write_text(str(deg))
    except OSError:
        pass
    return deg


def rotate_frame(frame, deg):
    """Rotate a numpy frame by the given degrees clockwise (no-op for 0)."""
    code = ROTATION_TO_CV2.get(deg)
    return frame if code is None else cv2.rotate(frame, code)


def rotated_size(size, deg):
    """(width, height) after rotation -- swapped for quarter turns."""
    w, h = size
    return (h, w) if deg in (90, 270) else (w, h)


CONFIG = load_config()
load_rotation()
STARTED_AT = time()

# --- camera setup (single shared instance) ---
picam2 = Picamera2()
config = picam2.create_video_configuration(
    # RGB888 hands back a BGR-ordered numpy array (a picamera2 quirk), which lines
    # up with ffmpeg's bgr24 below. If recorded colours look swapped, change the
    # ffmpeg input to -pix_fmt rgb24.
    main={"size": HIGH_RES, "format": "RGB888"},
    lores={"size": LOW_RES, "format": "YUV420"},
    controls={"FrameDurationLimits": (int(1_000_000 / FPS), int(1_000_000 / FPS))},
)
picam2.configure(config)
picam2.start()
sleep(2)

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
    """Convert lores YUV frame to JPEG (rotated to the current setting) and stash it for the stream."""
    global latest_jpeg
    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    bgr = rotate_frame(bgr, get_rotation())   # live rotation -- reflects the endpoint immediately
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
    if ok:
        with latest_jpeg_lock:
            latest_jpeg = buf.tobytes()
        new_frame_event.set()


def recorder_thread(final_path, frame_q, width, height):
    """Owns one clip's ffmpeg process. Pulls raw rotated frames off the queue, encodes
    H.264 + muxes the mic in a single pass, then makes a thumbnail. Runs off the motion
    loop so a slow encoder never stalls detection -- frames just drop at the queue."""
    proc = subprocess.Popen(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            # video in: raw rotated frames on stdin. Timestamp by arrival (wall clock) so
            # the track stays aligned with the live audio even if the capture loop drops frames.
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
            # audio in: straight off the mic
            "-f", "alsa", "-thread_queue_size", "1024", "-i", MIC_DEVICE,
            # one encode, broadly-compatible pixel format, end when the video (stdin) ends
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            final_path,
        ],
        stdin=subprocess.PIPE,
    )
    try:
        while True:
            item = frame_q.get()
            if item is None:          # sentinel: stop requested, drain done
                break
            try:
                proc.stdin.write(item)
            except (BrokenPipeError, ValueError):
                break                 # ffmpeg went away; bail and finalize whatever exists
    finally:
        try:
            proc.stdin.close()        # EOF on the video input -> ffmpeg writes the moov atom and exits
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if Path(final_path).exists():
            generate_thumbnail(final_path, THUMB_DIR / (Path(final_path).stem + ".jpg"))
        print(f"saved -> {Path(final_path).name}")


def motion_loop():
    """Background thread. Owns all camera reads. Lores drives motion detection + the live
    stream; while recording, main frames are rotated and handed to a recorder thread."""
    prev_frame = None
    recording = False
    record_started_at = 0.0
    last_motion_at = 0.0
    last_stream_update = 0.0
    frame_q = None
    clip_rotation = 0
    final_path = None

    print("Motion loop started.")
    while True:
        req = picam2.capture_request()
        try:
            lores = req.make_array("lores")
            gray = lores[:LOW_RES[1], :LOW_RES[0]]
            motion_pixels, prev_frame = detect_motion(prev_frame, gray)
            motion = motion_pixels > MOTION_THRESHOLD
            now = time()

            if motion:
                set_state(last_motion_at=now)

            # update stream JPEG at STREAM_FPS
            if now - last_stream_update > 1.0 / STREAM_FPS:
                update_stream_jpeg(lores)
                last_stream_update = now

            if recording:
                main = req.make_array("main")
                main = rotate_frame(main, clip_rotation)
                try:
                    frame_q.put_nowait(main.tobytes())
                except queue.Full:
                    pass  # encoder is behind; drop this frame rather than stall detection

                if motion:
                    last_motion_at = now
                quiet_for = now - last_motion_at
                recorded_for = now - record_started_at
                if quiet_for >= QUIET_SECONDS or recorded_for >= MAX_CLIP_SECONDS:
                    frame_q.put(None)  # signal the recorder to finalize (drains remaining frames first)
                    reason = "max length" if recorded_for >= MAX_CLIP_SECONDS else "quiet"
                    print(f"stopped ({reason}, {recorded_for:.1f}s)")
                    recording = False
                    frame_q = None
                    set_state(
                        recording=False,
                        recording_filename=None,
                        recording_started_at=None,
                    )
                    prev_frame = None
            else:
                if motion:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    final_path = f"{CAPTURE_DIR}/clip_{timestamp}.mp4"
                    clip_rotation = get_rotation()                  # snapshot: whole clip uses one rotation
                    out_w, out_h = rotated_size(HIGH_RES, clip_rotation)
                    frame_q = queue.Queue(maxsize=FRAME_QUEUE_MAX)
                    threading.Thread(
                        target=recorder_thread,
                        args=(final_path, frame_q, out_w, out_h),
                        daemon=True,
                    ).start()
                    record_started_at = now
                    last_motion_at = now
                    recording = True
                    set_state(
                        recording=True,
                        recording_filename=f"clip_{timestamp}.mp4",
                        recording_started_at=now,
                    )
                    print(f"motion: {motion_pixels} pixels, recording -> clip_{timestamp}.mp4")
        finally:
            req.release()


# --- Flask app ---
app = Flask(__name__)


def is_valid_clip_name(filename):
    """Reject anything that isn't a final clip, including path traversal."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    if not filename.startswith("clip_") or not filename.endswith(".mp4"):
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
    du = shutil.disk_usage(CAPTURE_DIR)
    clip_count = 0
    clip_bytes = 0
    for f in CAPTURE_DIR.glob("clip_*.mp4"):
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
        "rotation": get_rotation(),
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
    """Delete a clip and its thumbnail."""
    if not is_valid_clip_name(filename):
        return jsonify({"error": "invalid filename"}), 400

    target = CAPTURE_DIR / filename
    if not target.exists():
        return jsonify({"error": "not found"}), 404

    deleted = []
    target.unlink()
    deleted.append(target.name)
    thumb = THUMB_DIR / (target.stem + ".jpg")
    if thumb.exists():
        thumb.unlink()
        deleted.append(f".thumbnails/{thumb.name}")

    print(f"deleted: {deleted}")
    return jsonify({"deleted": deleted})


@app.route("/api/clips/batch_delete", methods=["POST"])
@require_token
def api_delete_clips_list():
    """Delete a provided list of clip filenames.

    Body: { "delete": ["clip1.mp4", "clip2.mp4"] }
    Returns: { "deleted": ["clip1.mp4", ...] }
    """
    data = request.get_json() or {}
    delete_list = data.get("delete", [])
    if not isinstance(delete_list, list):
        return jsonify({"error": "delete must be a list"}), 400

    deleted = []
    for name in delete_list:
        if not is_valid_clip_name(name):
            continue
        target = CAPTURE_DIR / name
        if not target.exists():
            continue
        target.unlink()
        thumb = THUMB_DIR / (target.stem + ".jpg")
        if thumb.exists():
            thumb.unlink()
        deleted.append(name)

    print(f"deleted list: {len(deleted)} clips")
    return jsonify({"deleted": deleted})


@app.route("/api/clips/names")
@require_token
def api_clips_names():
    """Return a JSON list of all clip filenames (newest first)."""
    clips, _ = list_clips(page=1, per_page=999999999)
    names = [name for name, _, _ in clips]
    return jsonify({"names": names})


@app.route("/api/rotation", methods=["GET"])
@require_token
def api_get_rotation():
    return jsonify({"rotation": get_rotation(), "options": [0, 90, 180, 270]})


@app.route("/api/rotation", methods=["POST"])
@require_token
def api_set_rotation():
    """Set image rotation. Body {"rotation": 0|90|180|270} or ?rotation=.
    Degrees are clockwise; 270 corrects a camera mounted 90 clockwise.
    Applies to the live stream immediately and to subsequent recordings
    (a clip already in progress keeps the rotation it started with)."""
    data = request.get_json(silent=True) or {}
    val = data.get("rotation", request.args.get("rotation"))
    if val is None:
        return jsonify({"error": "missing 'rotation'"}), 400
    try:
        deg = set_rotation(val)
    except (ValueError, TypeError):
        return jsonify({"error": "rotation must be 0, 90, 180, or 270"}), 400
    return jsonify({"rotation": deg})


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