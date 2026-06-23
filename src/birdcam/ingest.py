"""Recorder ingest engine. Pulls a camera node's low-res detect substream for motion + the
live stream, and records the node's already-encoded H.264 main stream straight to disk with
`ffmpeg -c copy` -- no decode, no re-encode. Reuses the shared motion detector and the shared
finaliser; the only new thing versus the standalone is that frames come off RTSP and the clip
is produced by an ffmpeg subprocess instead of the local hardware encoder. No picamera2."""
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from time import sleep, time

import cv2

from birdcam import state, recording
from birdcam.motion import detect_motion
from birdcam.config import (
    RTSP_MAIN, RTSP_DETECT, RTSP_AUDIO, STREAM_FPS, STREAM_QUALITY, MOTION_THRESHOLD,
    QUIET_SECONDS, MAX_CLIP_SECONDS, RECORD_COOLDOWN, MIN_FREE_MB, CAPTURE_DIR, TMP_DIR,
)


def _start_record(main_url, working_path):
    """Record the node's H.264 main stream to disk without touching the pixels. ffmpeg owns
    the RTSP pull + mp4 mux; we just start it and stop it. stdin is a pipe so we can send a
    graceful 'q' on stop (which lets ffmpeg write the moov atom and leave a playable file)."""
    return subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-rtsp_transport", "tcp", "-i", main_url,
         "-c", "copy", "-f", "mp4", working_path],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def _start_audio_record(audio_url, working_audio):
    """Pull the node's audio-only RTSP path to disk as ADTS AAC -- a streamable container that
    stays usable even if the process is killed (no moov atom to finalise). Runs independently
    of the video pull, so if audio is down the clip just comes out silent instead of failing.
    The node already encodes AAC, so this copies, never re-encodes."""
    return subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-rtsp_transport", "tcp", "-i", audio_url,
         "-vn", "-c:a", "copy", "-f", "adts", working_audio],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def _stop_proc(proc, graceful):
    if proc is None:
        return
    try:
        if graceful:
            proc.communicate(input=b"q", timeout=5)   # 'q' lets ffmpeg write the mp4 moov atom
        else:
            proc.terminate()
            proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _stop_and_finalize(video_proc, audio_proc, working_video, working_audio, final_path):
    """Stop both recorders, mux the audio sidecar into the video if we captured any, then hand
    the finished file to the shared finaliser. Runs on a background thread so the detect loop
    keeps delivering frames."""
    _stop_proc(video_proc, graceful=True)    # mp4 needs the graceful 'q' to finalise
    _stop_proc(audio_proc, graceful=False)   # ADTS is fine to just terminate
    source = working_video
    have_audio = (working_audio and Path(working_audio).exists()
                  and Path(working_audio).stat().st_size > 0)
    if working_video and have_audio:
        muxed = str(TMP_DIR / (Path(final_path).stem + ".muxed.mp4"))
        rc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", working_video, "-i", working_audio,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", "-f", "mp4", muxed]
        ).returncode
        if rc == 0 and Path(muxed).exists() and Path(muxed).stat().st_size > 0:
            recording._cleanup(working_video)
            source = muxed
        else:
            print(f"WARNING: audio mux failed ({rc}); keeping silent video", flush=True)
            recording._cleanup(muxed)
    recording.finalize_clip(source, final_path)
    recording._cleanup(working_audio)


def _detect_loop(cap):
    """Run motion + recording off one open detect stream. Returns when the stream ends so the
    caller can reconnect."""
    prev_frame = None
    recording_now = False
    record_started_at = 0.0
    last_motion_at = 0.0
    last_stream_update = 0.0
    rec_proc = None
    audio_proc = None
    working_path = None
    working_audio = None
    final_path = None
    finalize_th = None
    record_cooldown_until = 0.0

    def end_recording():
        nonlocal recording_now, rec_proc, audio_proc, working_path, working_audio, final_path, finalize_th, prev_frame
        finalize_th = threading.Thread(
            target=_stop_and_finalize,
            args=(rec_proc, audio_proc, working_path, working_audio, final_path), daemon=True,
        )
        finalize_th.start()
        recording_now = False
        rec_proc = None
        audio_proc = None
        working_path = None
        working_audio = None
        final_path = None
        prev_frame = None
        state.set_state(recording=False, recording_filename=None, recording_started_at=None)

    while True:
        ok, frame = cap.read()
        if not ok:
            print("ingest: detect stream ended", flush=True)
            if recording_now:
                end_recording()
            return
        try:
            state.beat()
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            motion_pixels, prev_frame = detect_motion(prev_frame, gray)
            motion = motion_pixels > MOTION_THRESHOLD
            now = time()

            if motion:
                state.set_state(last_motion_at=now)

            if now - last_stream_update > 1.0 / STREAM_FPS:
                ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
                if ok2:
                    state.set_jpeg(buf.tobytes())
                last_stream_update = now

            if recording_now:
                if rec_proc is not None and rec_proc.poll() is not None:
                    # the recording ffmpeg exited on its own (stream dropped, etc.)
                    print("ingest: recorder ffmpeg exited; stopping clip", flush=True)
                    end_recording()
                    record_cooldown_until = now + RECORD_COOLDOWN
                elif not state.recording_enabled():
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
                if motion and state.recording_enabled() and now >= record_cooldown_until and finalize_done:
                    free = recording.free_mb()
                    if free < MIN_FREE_MB:
                        free = recording.enforce_retention()
                    if free < MIN_FREE_MB:
                        print(f"low disk: {free:.0f} MB free, skipping recording", flush=True)
                        record_cooldown_until = now + RECORD_COOLDOWN
                    else:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        final_path = f"{CAPTURE_DIR}/clip_{timestamp}.mp4"
                        working_path = str(TMP_DIR / f"clip_{timestamp}.part")
                        try:
                            rec_proc = _start_record(RTSP_MAIN, working_path)
                        except OSError as e:
                            print(f"ingest: failed to start recorder: {e!r}", flush=True)
                            rec_proc = None
                            working_path = None
                            working_audio = None
                            final_path = None
                            record_cooldown_until = now + RECORD_COOLDOWN
                        else:
                            audio_proc = None
                            working_audio = None
                            if RTSP_AUDIO:
                                working_audio = str(TMP_DIR / f"clip_{timestamp}.aac")
                                try:
                                    audio_proc = _start_audio_record(RTSP_AUDIO, working_audio)
                                except OSError as e:
                                    print(f"ingest: audio recorder failed to start: {e!r}", flush=True)
                                    audio_proc = None
                                    working_audio = None
                            record_started_at = now
                            last_motion_at = now
                            recording_now = True
                            state.set_state(
                                recording=True,
                                recording_filename=f"clip_{timestamp}.mp4",
                                recording_started_at=now,
                            )
                            print(f"motion: {motion_pixels} pixels, recording -> clip_{timestamp}.mp4", flush=True)
        except Exception as e:
            print(f"ingest iteration error: {e!r}", flush=True)
            if recording_now:
                end_recording()


def run():
    """Open the detect stream and run the loop, reconnecting if the feed drops."""
    if not RTSP_MAIN:
        raise SystemExit(
            "recorder needs rtsp_main in /etc/birdcam/config.toml "
            "(e.g. rtsp_main = \"rtsp://sarkos:8554/cam\")"
        )
    detect_url = RTSP_DETECT or RTSP_MAIN
    print(f"ingest: detect={detect_url}  record={RTSP_MAIN}  audio={RTSP_AUDIO or '(none)'}", flush=True)
    while True:
        cap = cv2.VideoCapture(detect_url)
        if not cap.isOpened():
            print(f"ingest: cannot open {detect_url}; retrying in 2s", flush=True)
            sleep(2)
            continue
        try:
            _detect_loop(cap)
        finally:
            cap.release()
        sleep(1)