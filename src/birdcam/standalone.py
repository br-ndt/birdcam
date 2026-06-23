"""Standalone entrypoint: a self-contained node that captures, detects, records, and serves
the viewer -- the original single-box birdcam. Wires the shared web app to the picamera2
capture engine. Console script: `birdcam-standalone`."""
import threading

from birdcam import state, camera
from birdcam.config import PORT
from birdcam.rotation import load_rotation
from birdcam.web import app


def main():
    load_rotation()
    state.load_recording_enabled()
    camera.init_camera()
    threading.Thread(target=camera.motion_loop, daemon=True).start()
    threading.Thread(target=state.watchdog, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
