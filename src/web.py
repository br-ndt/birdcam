"""The Flask app and every route -- identical for the standalone and the recorder, since
both expose the same viewer API and serve the same live MJPEG buffer. Imports config/state/
rotation/recording but never an engine, so there's no import cycle; the entrypoints wire an
engine alongside this app."""
import shutil
from functools import wraps
from time import time

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from birdcam import __version__ as VERSION
from birdcam import state, rotation, recording
from birdcam.config import CAPTURE_DIR, THUMB_DIR, CONFIG, HIGH_RES, LOW_RES, STREAM_FPS

app = Flask(__name__)


def require_token(f):
    """Bearer-token auth. Accepts an Authorization header or a ?token= query param (the
    latter so an MJPEG <img> tag, which can't send headers, still works on a trusted LAN)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            token = request.args.get("token")
        if not token or token != CONFIG["token"]:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route("/health")
def health():
    """Unauthenticated liveness probe. No sensitive info."""
    return jsonify({"ok": True, "version": VERSION})


@app.route("/api/status")
@require_token
def api_status():
    s = state.get_state()
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
        "uptime_seconds": int(time() - state.STARTED_AT),
        "recording": s["recording"],
        "recording_enabled": state.recording_enabled(),
        "recording_filename": s["recording_filename"],
        "recording_started_at": s["recording_started_at"],
        "last_motion_at": s["last_motion_at"],
        "rotation": rotation.get_rotation(),
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
    page = max(1, int(request.args.get("page", 1)))
    per_page = 20
    clips, total = recording.list_clips(page=page, per_page=per_page)
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
    if not recording.is_valid_clip_name(filename):
        abort(404)
    return send_from_directory(CAPTURE_DIR.absolute(), filename)


@app.route("/api/clips/<filename>/thumbnail")
@require_token
def api_clip_thumbnail(filename):
    if not recording.is_valid_clip_name(filename):
        abort(404)
    clip_path = CAPTURE_DIR / filename
    if not clip_path.exists():
        abort(404)
    thumb_name = clip_path.stem + ".jpg"
    thumb_path = THUMB_DIR / thumb_name
    if not thumb_path.exists():
        if not recording.generate_thumbnail(clip_path, thumb_path):
            abort(500)
    return send_from_directory(THUMB_DIR.absolute(), thumb_name)


@app.route("/api/clips/<filename>", methods=["DELETE"])
@require_token
def api_delete_clip(filename):
    if not recording.is_valid_clip_name(filename):
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
    data = request.get_json() or {}
    delete_list = data.get("delete", [])
    if not isinstance(delete_list, list):
        return jsonify({"error": "delete must be a list"}), 400
    deleted = []
    for name in delete_list:
        if not recording.is_valid_clip_name(name):
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
    clips, _ = recording.list_clips(page=1, per_page=999999999)
    names = [name for name, _, _ in clips]
    return jsonify({"names": names})


@app.route("/api/rotation", methods=["GET"])
@require_token
def api_get_rotation():
    return jsonify({"rotation": rotation.get_rotation(), "options": list(rotation.VALID_ROTATIONS)})


@app.route("/api/rotation", methods=["POST"])
@require_token
def api_set_rotation():
    data = request.get_json(silent=True) or {}
    val = data.get("rotation", request.args.get("rotation"))
    if val is None:
        return jsonify({"error": "missing 'rotation'"}), 400
    try:
        deg = rotation.set_rotation(val)
    except (ValueError, TypeError):
        return jsonify({"error": "rotation must be 0, 90, 180, or 270"}), 400
    return jsonify({"rotation": deg})


@app.route("/api/recording", methods=["GET"])
@require_token
def api_get_recording():
    return jsonify({"recording_enabled": state.recording_enabled()})


@app.route("/api/recording", methods=["POST"])
@require_token
def api_set_recording():
    data = request.get_json(silent=True) or {}
    val = data.get("enabled", request.args.get("enabled"))
    if val is None:
        return jsonify({"error": "missing 'enabled'"}), 400
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "1", "yes", "on"):
            enabled = True
        elif v in ("false", "0", "no", "off"):
            enabled = False
        else:
            return jsonify({"error": "enabled must be true or false"}), 400
    else:
        enabled = bool(val)
    enabled = state.set_recording_enabled(enabled)
    print(f"recording {'enabled' if enabled else 'disabled'} via API", flush=True)
    return jsonify({"recording_enabled": enabled})


def mjpeg_generator():
    """Yields multipart MJPEG frames from the shared buffer the active engine fills."""
    while True:
        state.new_frame_event.wait(timeout=2)
        state.new_frame_event.clear()
        frame = state.get_jpeg()
        if frame is None:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")


@app.route("/stream.mjpg")
@require_token
def stream():
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")
