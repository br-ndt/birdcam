"""Standalone capture engine -- the only module that imports picamera2/libcamera. Owns the
camera, runs motion detection + the live stream off the lores frames, and records clips by
handing the main stream to the hardware H.264 encoder while capturing the ALSA mic in
parallel (picamera2 0.3.34's FfmpegOutput audio is PulseAudio-only) and muxing at finalize.
Nothing here is imported by the recorder."""
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from time import sleep, time

import cv2
from libcamera import controls
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

from birdcam import state, recording
from birdcam.motion import detect_motion
from birdcam.config import (
    LOW_RES, HIGH_RES, FPS, LENS_POSITION, MIC_DEVICE, RECORD_AUDIO, VIDEO_BITRATE,
    STREAM_FPS, STREAM_QUALITY, MOTION_THRESHOLD, QUIET_SECONDS, MAX_CLIP_SECONDS,
    RECORD_COOLDOWN, MIN_FREE_MB, CAPTURE_DIR, TMP_DIR,
)

picam2 = None
AUDIO_ENABLED = False


def _mic_available(device):
    """Probe the ALSA capture device once at startup so a missing/wrong mic degrades to a
    silent clip instead of nothing."""
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


def init_camera():
    """Open + start the camera, pin focus, probe the mic. Call once before motion_loop()."""
    global picam2, AUDIO_ENABLED
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
    AUDIO_ENABLED = RECORD_AUDIO and _mic_available(MIC_DEVICE)
    if RECORD_AUDIO and not AUDIO_ENABLED:
        print(f"WARNING: mic {MIC_DEVICE!r} unavailable; recording video-only", flush=True)


def _bitrate_bps(value):
    """Accept 4_000_000, '4M', or '4000k' and return int bits-per-second for H264Encoder."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    mult = 1
    if s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("k"):
        mult, s = 1_000, s[:-1]
    return int(float(s) * mult)


def update_stream_jpeg(yuv):
    """Convert a lores YUV frame to JPEG (native orientation) and publish it to the stream."""
    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
    if ok:
        state.set_jpeg(buf.tobytes())


def _stop_and_finalize(audio_proc, working_video, working_audio, final_path):
    """Stop the encoder + mic, mux audio into the video if we captured any, then hand the
    finished file to the shared finaliser. Runs on a background thread."""
    try:
        picam2.stop_encoder()
    except Exception as e:
        print(f"stop_encoder error: {e!r}", flush=True)
    if audio_proc is not None:
        try:
            audio_proc.terminate()
            audio_proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                audio_proc.kill()
            except OSError:
                pass
    source = working_video
    have_audio = (working_audio and Path(working_audio).exists()
                  and Path(working_audio).stat().st_size > 0)
    if working_video and have_audio:
        muxed = str(TMP_DIR / (Path(final_path).stem + ".part"))
        rc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", working_video, "-i", working_audio,
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest",
             "-f", "mp4", muxed]
        ).returncode
        if rc == 0 and Path(muxed).exists():
            recording._cleanup(working_video)
            source = muxed
        else:
            print(f"WARNING: mux failed ({rc}); keeping silent video", flush=True)
            recording._cleanup(muxed)
    recording.finalize_clip(source, final_path)
    recording._cleanup(working_audio)


def motion_loop():
    """Owns all camera reads. Lores drives motion + the live stream; recording is handed to
    the hardware encoder, which pulls main itself, so a clip never starves the stream."""
    prev_frame = None
    recording_now = False
    record_started_at = 0.0
    last_motion_at = 0.0
    last_stream_update = 0.0
    encoder = None
    audio_proc = None
    working_video = None
    working_audio = None
    final_path = None
    finalize_th = None
    record_cooldown_until = 0.0

    def end_recording():
        nonlocal recording_now, encoder, audio_proc, working_video, working_audio, final_path, finalize_th, prev_frame
        finalize_th = threading.Thread(
            target=_stop_and_finalize,
            args=(audio_proc, working_video, working_audio, final_path), daemon=True,
        )
        finalize_th.start()
        recording_now = False
        encoder = None
        audio_proc = None
        working_video = None
        working_audio = None
        final_path = None
        prev_frame = None
        state.set_state(recording=False, recording_filename=None, recording_started_at=None)

    print("Motion loop started.", flush=True)
    while True:
        try:
            req = picam2.capture_request()
        except Exception as e:
            print(f"capture_request failed: {e!r}", flush=True)
            sleep(0.1)
            continue
        try:
            lores = req.make_array("lores").copy()
        except Exception as e:
            print(f"motion_loop iteration error: {e!r}", flush=True)
            req.release()
            continue
        # Release immediately: we hold our own lores copy, so freeing the buffers now (rather
        # than after motion + JPEG) keeps the hardware encoder fed and the clip full-length.
        req.release()
        try:
            state.beat()
            gray = lores[:LOW_RES[1], :LOW_RES[0]]
            motion_pixels, prev_frame = detect_motion(prev_frame, gray)
            motion = motion_pixels > MOTION_THRESHOLD
            now = time()

            if motion:
                state.set_state(last_motion_at=now)

            if now - last_stream_update > 1.0 / STREAM_FPS:
                update_stream_jpeg(lores)
                last_stream_update = now

            if recording_now:
                if not state.recording_enabled():
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
                        working_video = str(TMP_DIR / f"clip_{timestamp}.video.mp4")
                        working_audio = str(TMP_DIR / f"clip_{timestamp}.audio.wav")
                        encoder = H264Encoder(bitrate=_bitrate_bps(VIDEO_BITRATE))
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
                            recording_now = True
                            state.set_state(
                                recording=True,
                                recording_filename=f"clip_{timestamp}.mp4",
                                recording_started_at=now,
                            )
                            print(f"motion: {motion_pixels} pixels, recording -> clip_{timestamp}.mp4", flush=True)
        except Exception as e:
            print(f"motion_loop iteration error: {e!r}", flush=True)
            if recording_now:
                end_recording()
