"""
Bird feeder cam: motion-triggered A/V capture + live MJPEG stream + web viewer.
All in one process so we share a single camera instance.
"""
import logging
from flask import Flask, send_from_directory, abort, Response, jsonify, request
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
import cv2
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from time import sleep, time

# quiet down Flask's request logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# --- config ---
CAPTURE_DIR = Path("captures")
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

CAPTURE_DIR.mkdir(exist_ok=True)

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

# --- shared state for the live stream ---
latest_jpeg = None
latest_jpeg_lock = threading.Lock()
new_frame_event = threading.Event()


def merge_async(video_path, audio_path, final_path):
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
        else:
            print(f"  merge failed for {final_path}")
    threading.Thread(target=_do, daemon=True).start()


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
    # convert YUV420 to BGR for color JPEG
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
                merge_async(video_path, audio_path, final_path)
                reason = "max length" if recorded_for >= MAX_CLIP_SECONDS else "quiet"
                print(f"stopped ({reason}, {recorded_for:.1f}s)")
                recording = False
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
                print(f"motion: {motion_pixels} pixels, recording -> clip_{timestamp}.mp4")

        sleep(LOOP_DELAY)


# --- Flask app ---
app = Flask(__name__)


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


@app.route("/api/clips")
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
def serve_clip(filename):
    if not filename.startswith("clip_") or not filename.endswith(".mp4"):
        abort(404)
    if filename.endswith(".video.mp4"):
        abort(404)
    return send_from_directory(CAPTURE_DIR.absolute(), filename)


@app.route("/api/clips/<filename>", methods=["DELETE"])
def api_delete_clip(filename):
    """Delete a clip and its intermediate files."""
    # validate filename to prevent path traversal
    if not filename.startswith("clip_") or not filename.endswith(".mp4"):
        return jsonify({"error": "invalid filename"}), 400
    if filename.endswith(".video.mp4"):
        return jsonify({"error": "invalid filename"}), 400
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "invalid filename"}), 400

    target = CAPTURE_DIR / filename
    if not target.exists():
        return jsonify({"error": "not found"}), 404

    # delete the merged file plus any leftover intermediates
    base = filename[:-4]  # strip .mp4
    deleted = []
    for suffix in [".mp4", ".video.mp4", ".audio.wav"]:
        f = CAPTURE_DIR / f"{base}{suffix}"
        if f.exists():
            f.unlink()
            deleted.append(f.name)

    print(f"deleted: {deleted}")
    return jsonify({"deleted": deleted})


def mjpeg_generator():
    """Yields multipart MJPEG frames as bytes."""
    while True:
        # wait for a new frame, but don't block forever
        new_frame_event.wait(timeout=2)
        new_frame_event.clear()
        with latest_jpeg_lock:
            frame = latest_jpeg
        if frame is None:
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")


@app.route("/stream.mjpg")
def stream():
    return Response(
        mjpeg_generator(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


if __name__ == "__main__":
    # start motion detection thread
    t = threading.Thread(target=motion_loop, daemon=True)
    t.start()
    # run Flask (single-threaded is fine for our 1-2 viewers)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
