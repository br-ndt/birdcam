"""Shared runtime state, with no heavy imports so any engine or the web layer can use it
without creating an import cycle. Holds the status snapshot, the live MJPEG buffer, the
recording master switch, and the watchdog heartbeat."""
import os
import threading
from time import sleep, time

from birdcam.config import RECORDING_FLAG_FILE, HEARTBEAT_TIMEOUT

STARTED_AT = time()

# --- live MJPEG buffer: the active engine writes it, the web /stream.mjpg reads it ---
_latest_jpeg = None
_jpeg_lock = threading.Lock()
new_frame_event = threading.Event()


def set_jpeg(buf):
    global _latest_jpeg
    with _jpeg_lock:
        _latest_jpeg = buf
    new_frame_event.set()


def get_jpeg():
    with _jpeg_lock:
        return _latest_jpeg


# --- status snapshot exposed by /api/status ---
_state_lock = threading.Lock()
_state = {
    "recording": False,
    "recording_filename": None,
    "recording_started_at": None,
    "last_motion_at": None,
}


def set_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def get_state():
    with _state_lock:
        return dict(_state)


# --- recording master switch (persisted across restarts) ---
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
    global _recording_enabled
    enabled = bool(enabled)
    with _recording_lock:
        _recording_enabled = enabled
    try:
        RECORDING_FLAG_FILE.write_text("1" if enabled else "0")
    except OSError:
        pass
    return enabled


# --- watchdog: the active engine calls beat() each delivered frame ---
_last_loop_at = time()


def beat():
    global _last_loop_at
    _last_loop_at = time()


def watchdog():
    """If the active engine stops delivering frames -- silent thread death, a deadlock, a
    wedged camera, or a dead RTSP feed -- log loudly and exit so systemd restarts us. Needs
    Restart=always (or on-failure) in the unit to actually recover."""
    while True:
        sleep(HEARTBEAT_TIMEOUT / 2)
        stalled = time() - _last_loop_at
        if stalled > HEARTBEAT_TIMEOUT:
            print(f"WATCHDOG: capture loop stalled {stalled:.0f}s, exiting for restart", flush=True)
            os._exit(1)
