"""Recorder entrypoint: ingests one or more camera nodes over RTSP, runs motion detection on
the detect substream, records the main stream with `ffmpeg -c copy`, and serves the same
viewer API + live stream as the standalone. Imports the ingest engine, NOT the camera engine,
so this process never needs picamera2/libcamera. Console script: `birdcam-recorder`."""
import threading

from birdcam import state, ingest
from birdcam.config import PORT
from birdcam.rotation import load_rotation
from birdcam.web import app


def main():
    load_rotation()
    state.load_recording_enabled()
    threading.Thread(target=ingest.run, daemon=True).start()
    threading.Thread(target=state.watchdog, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
