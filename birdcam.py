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
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

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
HEARTBEAT_TIMEOUT   = CONFIG.get("heartbeat_timeout", 30)
MIN_FREE_MB         = CONFIG.get("min_free_mb", 500)
RETENTION_TARGET_MB = CONFIG.get("retention_target_mb", 1000)
RECORD_COOLDOWN     = CONFIG.get("record_cooldown", 5)
VIDEO_BITRATE       = CONFIG.get("video_bitrate", "4M")  # H264Encoder bitrate; accepts "4M"/"4000k"/int
RECORD_AUDIO        = CONFIG.get("record_audio", True)   # set false to force video-only (skips the probe)


def _mic_available(device):
    """Probe the capture device once at startup. FfmpegOutput(audio=True) aborts the whole
    recording -- video included -- if the mic can't be opened, so when it's missing or wrong
    we fall back to video-only instead of silently discarding every clip."""
    if not device:
        return False
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "alsa", "-i", str(device), "-t", "0.2", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


AUDIO_ENABLED = RECORD_AUDIO and _mic_available(MIC_DEVICE)
if RECORD_AUDIO and not AUDIO_ENABLED:
    print(f"WARNING: mic {MIC_DEVICE!r} unavailable; recording video-only", flush=True)

load_rotation()
load_recording_enabled()
STARTED_AT = time()

# --- camera setup (single shared instance) ---
picam2 = Picamera2()
config = picam2.create_video_configuration(
    # main feeds the hardware H.264 encoder directly; YUV420 is its native input.
    # lores drives motion detection + the MJPEG stream.
    main={"size": HIGH_RES, "format": "YUV420"},
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
    """Convert lores YUV frame to JPEG and stash it for the stream. The frame is left in
    native sensor orientation; the viewer applies rotation for display."""
    global latest_jpeg
    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
    if ok:
        with latest_jpeg_lock:
            latest_jpeg = buf.tobytes()
        new_frame_event.set()


def _bitrate_bps(value):
    """Accept 4_000_000, '4M', or '4000k' and return an int bits-per-second for H264Encoder."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    mult = 1
    if s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("k"):
        mult, s = 1_000, s[:-1]
    return int(float(s) * mult)


def _stop_and_finalize(audio_proc, working_video, working_audio, final_path):
    """Stop the hardware video encoder and the mic capture, then mux + finalise on this
    (background) thread so the capture loop keeps delivering frames immediately."""
    try:
        picam2.stop_encoder()
    except Exception as e:
        print(f"stop_encoder error: {e!r}", flush=True)
    if audio_proc is not None:
        # arecord finalises the wav and frees the mic on SIGTERM
        try:
            audio_proc.terminate()
            audio_proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                audio_proc.kill()
            except OSError:
                pass
    record_finalize(working_video, working_audio, final_path)


def record_finalize(working_video, working_audio, final_path):
    """Mux the hardware-encoded video (copy) with the mic audio (to AAC) into the final clip,
    build a thumbnail, then atomically rename so the UI only ever sees a finished, playable
    file. Tolerates missing/empty audio -- a silent clip beats no clip."""
    if not working_video or not final_path:
        return
    vp = Path(working_video)
    if not vp.exists() or vp.stat().st_size == 0:
        print(f"WARNING: no video for {Path(final_path).name}; discarding", flush=True)
        _cleanup(working_video, working_audio)
        return
    have_audio = bool(working_audio) and Path(working_audio).exists() and Path(working_audio).stat().st_size > 0
    source = working_video
    if have_audio:
        muxed = str(TMP_DIR / (Path(final_path).stem + ".part"))
        rc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", working_video, "-i", working_audio,
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest",
             "-f", "mp4", muxed]
        ).returncode
        if rc == 0 and Path(muxed).exists():
            _cleanup(working_video)
            source = muxed
        else:
            print(f"WARNING: mux failed ({rc}) for {Path(final_path).name}; keeping silent video", flush=True)
            _cleanup(muxed)
    generate_thumbnail(source, THUMB_DIR / (Path(final_path).stem + ".jpg"))
    try:
        os.rename(source, final_path)
        print(f"saved -> {Path(final_path).name}", flush=True)
    except OSError as e:
        print(f"WARNING: finalize failed for {Path(final_path).name}: {e!r}", flush=True)
        _cleanup(source)
    _cleanup(working_audio)


def _cleanup(*paths):
    for p in paths:
        try:
            Path(p).unlink()
        except OSError:
            pass


def motion_loop():
    """Background thread. Owns all camera reads. Lores drives motion detection + the live
    stream; recording is handed to picamera2's hardware H264Encoder, which pulls the main
    stream itself -- the loop never touches main, so a clip can't starve the camera or the
    stream. Every delivered frame updates the watchdog heartbeat, and the body is guarded so
    a single bad frame (or a transient camera error) can't silently kill the thread."""
    global _last_loop_at
    prev_frame = None
    recording = False
    record_started_at = 0.0
    last_motion_at = 0.0
    last_stream_update = 0.0
    encoder = None
    audio_proc = None
    working_video = None
    working_audio = None
    final_path = None
    finalize_th = None              # the previous clip's stop/finalise; the next clip waits on it
    record_cooldown_until = 0.0     # suppress retries for a few seconds after a failure/skip

    def end_recording():
        nonlocal recording, encoder, audio_proc, working_video, working_audio, final_path, finalize_th, prev_frame
        # stop + finalise off-thread so the mic is freed and the loop keeps delivering frames
        finalize_th = threading.Thread(
            target=_stop_and_finalize,
            args=(audio_proc, working_video, working_audio, final_path), daemon=True,
        )
        finalize_th.start()
        recording = False
        encoder = None
        audio_proc = None
        working_video = None
        working_audio = None
        final_path = None
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
                else:
                    if motion:
                        last_motion_at = now
                    quiet_for = now - last_motion_at
                    recorded_for = now - record_started_at
                    if quiet_for >= QUIET_SECONDS or recorded_for >= MAX_CLIP_SECONDS:
                        reason = "max length" if recorded_for >= MAX_CLIP_SECONDS else "quiet"
                        print(f"stopped ({reason}, {recorded_for:.1f}s)", flush=True)
                        end_recording()
            else:
                finalize_done = finalize_th is None or not finalize_th.is_alive()
                if motion and recording_enabled() and now >= record_cooldown_until and finalize_done:
                    free = free_mb()
                    if free < MIN_FREE_MB:
                        free = enforce_retention()      # prune oldest clips to make room
                    if free < MIN_FREE_MB:
                        # nothing left to prune and still no room -- skip rather than start a clip we
                        # can't finish (a full disk would stall the encoder and hold the mic)
                        print(f"low disk: {free:.0f} MB free, skipping recording", flush=True)
                        record_cooldown_until = now + RECORD_COOLDOWN
                    else:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        final_path = f"{CAPTURE_DIR}/clip_{timestamp}.mp4"
                        working_video = str(TMP_DIR / f"clip_{timestamp}.video.mp4")
                        working_audio = str(TMP_DIR / f"clip_{timestamp}.audio.wav")
                        encoder = H264Encoder(bitrate=_bitrate_bps(VIDEO_BITRATE))
                        # picamera2 0.3.34's FfmpegOutput audio is PulseAudio-only, so the ALSA
                        # mic is captured separately by arecord and muxed in at finalize.
                        encoder.output = FfmpegOutput(working_video, audio=False)
                        try:
                            picam2.start_encoder(encoder, name="main")
                        except Exception as e:
                            print(f"start_encoder failed: {e!r}", flush=True)
                            encoder = None
                            working_video = None
                            working_audio = None
                            final_path = None
                            record_cooldown_until = now + RECORD_COOLDOWN
                        else:
                            audio_proc = None
                            if AUDIO_ENABLED:
                                try:
                                    audio_proc = subprocess.Popen(
                                        ["arecord", "-D", MIC_DEVICE, "-f", "cd", "-q", working_audio],
                                        stderr=subprocess.DEVNULL,
                                    )
                                except OSError as e:
                                    print(f"arecord failed to start: {e!r}", flush=True)
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