"""
Bird feeder cam: motion-triggered A/V capture + live MJPEG stream + web viewer.

All in one process so we share a single camera instance.

Rotation is handled in software. A single `rotation` setting
(0/90/180/270 degrees clockwise, settable at runtime via /api/rotation) is
applied to every frame before it is used anywhere. The recording path captures
main frames in Python, rotates them, and pipes them to a single ffmpeg that
encodes H.264 and muxes the mic audio in one pass -- no intermediate files, no
second re-encode, no rotation-metadata flag to hope a player honours.
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

CAPTURE_DIR = Path("captures")
THUMB_DIR = CAPTURE_DIR / ".thumbnails"
TMP_DIR = CAPTURE_DIR / ".tmp"
ROTATION_FILE = CAPTURE_DIR / ".rotation"
RECORDING_FLAG_FILE = CAPTURE_DIR / ".recording_enabled"
DEFAULT_ROTATION = 0     # degrees clockwise; 270 == 90 CCW, which corrects a camera mounted 90 clockwise.

CONFIG_PATHS = [
    Path("/etc/birdcam/config.toml"),
    Path.home() / ".config/birdcam/config.toml",
    Path("birdcam.toml"),
]

CAPTURE_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)
TMP_DIR.mkdir(exist_ok=True)

# clear any video/audio/clip temporaries orphaned by a previous crash or kill (including
# legacy .part files that earlier versions wrote alongside clips)
for _stale in list(TMP_DIR.iterdir()) + list(CAPTURE_DIR.glob("*.part")):
    try:
        _stale.unlink()
    except OSError:
        pass


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


# --- recording master switch: pauses motion-triggered clips; the live stream is unaffected ---
_recording_lock = threading.Lock()
_recording_enabled = True


def load_recording_enabled():
    global _recording_enabled
    try:
        _recording_enabled = RECORDING_FLAG_FILE.read_text().strip() != "0"
    except OSError:
        pass


def recording_enabled():
    with _recording_lock:
        return _recording_enabled


def set_recording_enabled(enabled):
    """Enable/disable motion-triggered clip recording. Persists across restarts."""
    global _recording_enabled
    enabled = bool(enabled)
    with _recording_lock:
        _recording_enabled = enabled
    try:
        RECORDING_FLAG_FILE.write_text("1" if enabled else "0")
    except OSError:
        pass
    return enabled


CONFIG = load_config()

LOW_RES             = tuple(CONFIG.get("low_res", (640, 480)))
HIGH_RES            = tuple(CONFIG.get("high_res", (1920, 1080)))
FPS                 = CONFIG.get("fps", 30)
MIC_DEVICE          = CONFIG.get("mic_device", "plughw:2,0")
MOTION_THRESHOLD    = CONFIG.get("motion_threshold", 5000)
QUIET_SECONDS       = CONFIG.get("quiet_seconds", 3)
MAX_CLIP_SECONDS    = CONFIG.get("max_clip_seconds", 120)
PORT                = CONFIG.get("port", 5000)
STREAM_FPS          = CONFIG.get("stream_fps", 10)
STREAM_QUALITY      = CONFIG.get("stream_quality", 80)
THUMB_WIDTH         = CONFIG.get("thumb_width", 320)
LENS_POSITION       = CONFIG.get("lens_position", 1.4)
FRAME_QUEUE_MAX     = CONFIG.get("frame_queue_max", FPS * 2)
HEARTBEAT_TIMEOUT   = CONFIG.get("heartbeat_timeout", 30)
MIN_FREE_MB         = CONFIG.get("min_free_mb", 500)
RETENTION_TARGET_MB = CONFIG.get("retention_target_mb", 1000)
RECORD_COOLDOWN     = CONFIG.get("record_cooldown", 5)
KILL_GRACE          = CONFIG.get("kill_grace", 10)
VIDEO_ENCODER       = CONFIG.get("video_encoder", "h264_v4l2m2m" if os.path.exists("/dev/video11") else "libx264")
VIDEO_BITRATE       = CONFIG.get("video_bitrate", "4M")
VIDEO_CRF           = str(CONFIG.get("video_crf", 23))

load_rotation()
load_recording_enabled()
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
# Module 3: pin focus for a static feeder so autofocus can't hunt onto the housing.
picam2.set_controls({"AfMode": controls.AfModeEnum.Manual, "LensPosition": LENS_POSITION})

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


# updated after each delivered frame in motion_loop; the watchdog reads it to detect a wedged pipeline
_last_loop_at = time()


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


def free_mb():
    return shutil.disk_usage(CAPTURE_DIR).free / (1024 * 1024)


def enforce_retention():
    """Delete oldest clips (+ their thumbnails) until free space is back above the target.
    Returns the resulting free MB. A no-op when there's already room."""
    if free_mb() >= RETENTION_TARGET_MB:
        return free_mb()
    for clip in sorted(CAPTURE_DIR.glob("clip_*.mp4"), key=lambda p: p.stat().st_mtime):
        if free_mb() >= RETENTION_TARGET_MB:
            break
        try:
            clip.unlink()
            thumb = THUMB_DIR / (clip.stem + ".jpg")
            if thumb.exists():
                thumb.unlink()
            print(f"retention: pruned {clip.name}", flush=True)
        except OSError:
            pass
    return free_mb()


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


def recorder_thread(final_path, frame_q, width, height, stop_event):
    """One clip. Video frames (already rotated) are piped into a VIDEO-ONLY ffmpeg; audio is
    captured separately by arecord. Keeping the mic out of the encoder means the encoder can
    never hold the device -- arecord owns it and releases it promptly on stop, so a clip can't
    lock the mic against the next one. On stop the two temp files are muxed (copy) into the
    final clip on a background thread, so this thread returns as soon as the mic is freed."""
    stem = Path(final_path).stem
    tmp_video = str(TMP_DIR / f"{stem}.video.mp4")
    tmp_audio = str(TMP_DIR / f"{stem}.audio.wav")

    if VIDEO_ENCODER == "h264_v4l2m2m":
        enc_args = ["-c:v", "h264_v4l2m2m", "-b:v", VIDEO_BITRATE]
    else:
        enc_args = ["-c:v", VIDEO_ENCODER, "-preset", "veryfast", "-crf", VIDEO_CRF]


    video = subprocess.Popen(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            # raw rotated frames on stdin, timestamped by arrival so dropped frames don't desync
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
            *enc_args, "-pix_fmt", "yuv420p",
            "-f", "mp4", tmp_video,
        ],
        stdin=subprocess.PIPE,
    )
    audio = subprocess.Popen(
        ["arecord", "-D", MIC_DEVICE, "-f", "cd", "-q", tmp_audio],
        stderr=subprocess.DEVNULL,
    )

    # Backstop: never let either child outlive the clip and hold the mic or the pipe.
    def _kill_both():
        for p in (video, audio):
            try:
                p.kill()
            except OSError:
                pass
    killer = threading.Timer(MAX_CLIP_SECONDS + KILL_GRACE, _kill_both)
    killer.daemon = True
    killer.start()

    def _write(item):
        try:
            video.stdin.write(item)
            return True
        except (BrokenPipeError, ValueError):
            return False

    try:
        while not stop_event.is_set():
            try:
                item = frame_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not _write(item):
                break
        while True:                       # drain the tail of the clip
            try:
                item = frame_q.get_nowait()
            except queue.Empty:
                break
            if not _write(item):
                break
    finally:
        killer.cancel()
        # Release the mic FIRST so the next clip can start immediately: arecord finalises the
        # wav and frees plughw on SIGTERM.
        try:
            audio.terminate()
            audio.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                audio.kill()
            except OSError:
                pass
        # Finish the video: EOF on stdin -> ffmpeg finalises and exits (fast; no live input).
        try:
            video.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            video.wait(timeout=10)
        except subprocess.TimeoutExpired:
            video.kill()
        # Mux on a background thread so the mic stays free and the loop can record again now.
        threading.Thread(
            target=_mux_and_finalize, args=(final_path, tmp_video, tmp_audio), daemon=True
        ).start()


def _mux_and_finalize(final_path, tmp_video, tmp_audio):
    """Mux the temp video (copy) + audio (to AAC) into the final clip, build the thumbnail,
    then atomically rename so the UI only ever sees a finished, playable file. Tolerates a
    missing/empty audio file -- a clip with no sound still beats no clip."""
    working_path = str(TMP_DIR / (Path(final_path).stem + ".part"))
    have_video = Path(tmp_video).exists() and Path(tmp_video).stat().st_size > 0
    have_audio = Path(tmp_audio).exists() and Path(tmp_audio).stat().st_size > 0
    if not have_video:
        print(f"WARNING: no video for {Path(final_path).name}; discarding", flush=True)
        _cleanup(tmp_video, tmp_audio)
        return

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_video]
    if have_audio:
        cmd += ["-i", tmp_audio, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-f", "mp4", working_path]

    rc = subprocess.run(cmd).returncode
    if rc == 0 and Path(working_path).exists():
        generate_thumbnail(working_path, THUMB_DIR / (Path(final_path).stem + ".jpg"))
        os.rename(working_path, final_path)
        print(f"saved -> {Path(final_path).name}", flush=True)
    else:
        print(f"WARNING: mux failed ({rc}) for {Path(final_path).name}; discarding", flush=True)
        _cleanup(working_path)
    _cleanup(tmp_video, tmp_audio)


def _cleanup(*paths):
    for p in paths:
        try:
            Path(p).unlink()
        except OSError:
            pass


def motion_loop():
    """Background thread. Owns all camera reads. Lores drives motion detection + the live
    stream; while recording, main frames are rotated and handed to a recorder thread.
    Every delivered frame updates the watchdog heartbeat, and the body is guarded so a
    single bad frame (or a transient camera error) can't silently kill the thread."""
    global _last_loop_at
    prev_frame = None
    recording = False
    record_started_at = 0.0
    last_motion_at = 0.0
    last_stream_update = 0.0
    frame_q = None
    stop_event = None
    recorder_th = None
    clip_rotation = 0
    final_path = None
    record_cooldown_until = 0.0     # suppress retries for a few seconds after a failure/skip

    def end_recording(signal_stop=True):
        nonlocal recording, frame_q, stop_event, recorder_th, prev_frame
        if signal_stop and stop_event is not None:
            stop_event.set()          # non-blocking; the recorder drains + finalizes itself
        recording = False
        frame_q = None
        stop_event = None
        # keep the recorder_th reference: a new clip must wait until it has fully exited
        # (mic released) before opening the device again
        prev_frame = None
        set_state(recording=False, recording_filename=None, recording_started_at=None)

    print("Motion loop started.", flush=True)
    while True:
        try:
            req = picam2.capture_request()
        except Exception as e:
            # don't update the heartbeat -- if this keeps failing, the watchdog restarts us
            print(f"capture_request failed: {e!r}", flush=True)
            sleep(0.1)
            continue
        try:
            lores = req.make_array("lores")
            _last_loop_at = time()          # heartbeat: the camera delivered a frame
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
                if not recording_enabled():
                    # master switch turned off mid-clip -- finish the current clip cleanly
                    print("recording disabled; stopping current clip", flush=True)
                    end_recording()
                elif recorder_th is not None and not recorder_th.is_alive():
                    # ffmpeg/ALSA failure took the recorder down -- stop cleanly instead of
                    # spending the next QUIET_SECONDS writing into a dead queue
                    print("recorder thread ended early; stopping clip", flush=True)
                    end_recording(signal_stop=False)
                    record_cooldown_until = now + RECORD_COOLDOWN
                else:
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
                        reason = "max length" if recorded_for >= MAX_CLIP_SECONDS else "quiet"
                        print(f"stopped ({reason}, {recorded_for:.1f}s)", flush=True)
                        end_recording()
            else:
                recorder_done = recorder_th is None or not recorder_th.is_alive()
                if motion and recording_enabled() and now >= record_cooldown_until and recorder_done:
                    free = free_mb()
                    if free < MIN_FREE_MB:
                        free = enforce_retention()      # prune oldest clips to make room
                    if free < MIN_FREE_MB:
                        # nothing left to prune and still no room -- skip rather than stall ffmpeg
                        # on a full disk (which would block the write and hold the mic)
                        print(f"low disk: {free:.0f} MB free, skipping recording", flush=True)
                        record_cooldown_until = now + RECORD_COOLDOWN
                    else:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        final_path = f"{CAPTURE_DIR}/clip_{timestamp}.mp4"
                        clip_rotation = get_rotation()              # snapshot: whole clip uses one rotation
                        out_w, out_h = rotated_size(HIGH_RES, clip_rotation)
                        frame_q = queue.Queue(maxsize=FRAME_QUEUE_MAX)
                        stop_event = threading.Event()
                        recorder_th = threading.Thread(
                            target=recorder_thread,
                            args=(final_path, frame_q, out_w, out_h, stop_event),
                            daemon=True,
                        )
                        recorder_th.start()
                        record_started_at = now
                        last_motion_at = now
                        recording = True
                        set_state(
                            recording=True,
                            recording_filename=f"clip_{timestamp}.mp4",
                            recording_started_at=now,
                        )
                        print(f"motion: {motion_pixels} pixels, recording -> clip_{timestamp}.mp4", flush=True)
        except Exception as e:
            # one bad frame shouldn't take down the loop; log it and keep going
            print(f"motion_loop iteration error: {e!r}", flush=True)
            if recording:
                end_recording()
        finally:
            req.release()


def watchdog():
    """If the capture loop stops delivering frames -- silent thread death, a deadlock, or a
    wedged camera -- log loudly and exit so systemd restarts us cleanly. Requires
    Restart=always (or on-failure) in the unit file to actually recover."""
    while True:
        sleep(HEARTBEAT_TIMEOUT / 2)
        stalled = time() - _last_loop_at
        if stalled > HEARTBEAT_TIMEOUT:
            print(f"WATCHDOG: capture loop stalled {stalled:.0f}s, exiting for restart", flush=True)
            os._exit(1)


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
        "recording_enabled": recording_enabled(),
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


@app.route("/api/recording", methods=["GET"])
@require_token
def api_get_recording():
    return jsonify({"recording_enabled": recording_enabled()})


@app.route("/api/recording", methods=["POST"])
@require_token
def api_set_recording():
    """Turn motion-triggered clip recording on or off. The live stream is unaffected, and
    turning it off also stops any clip currently in progress. The setting persists across
    restarts. Body {"enabled": true|false} or ?enabled=true/false/1/0."""
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
    print(enabled)
    enabled = set_recording_enabled(enabled)
    print(f"recording {'enabled' if enabled else 'disabled'} via API", flush=True)
    return jsonify({"recording_enabled": enabled})


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
    threading.Thread(target=motion_loop, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)