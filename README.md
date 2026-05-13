<img src="frontend/src/assets/logo.png" width="128" height="128" style="border-radius: 100%;">

# birdcam

A Raspberry Pi-based camera package. Watches for motion, records variable-length A/V clips with synchronized audio, streams a live preview, and serves a React-based viewer for browsing captures.

## Hardware

- Raspberry Pi 5 (2GB or 4GB)
- MicroSD card (32GB+)
- Compatible camera
- Compatible microphone

This project is built for the Raspberry Pi family running Raspberry 
Pi OS (or other Debian-based distros). It depends on `picamera2` for 
camera access, which is Pi-specific. Porting to other hardware (USB 
webcams, IP cameras) is doable but not currently supported — the 
camera abstraction in `birdcam.py` would need to be factored out.

System dependencies (`apt`, `systemd`, `alsa-utils`, `ffmpeg`) are 
standard on Debian-based Linux but would need translation for other 
distros.

## Software Requirements

- apt
- python3
- systemd (optional)
- node/npm (optional)

## Setup

### System dependencies
```bash
sudo apt install -y ffmpeg alsa-utils libcap-dev
```

### Install python dependencies
We recommend using [uv](https://github.com/astral-sh/uv) or a standard python `venv` to manage dependencies.

**Using uv:**
```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

**Using standard venv:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Find your microphone's ALSA card number

```bash
arecord -l
```

Note the card number (e.g. `card 2`). Edit `birdcam.py` and update `MIC_DEVICE = "plughw:2,0"` if the number is different.

### Test manually

```bash
# make sure your virtual environment is activated
source .venv/bin/activate
BIRDCAM_TOKEN=abc123 python3 birdcam.py
```

Visit `http://{hostname}:5000/stream.mjpg` and you should see the silent live video of your camera. Wave hello!

Additionally, if all is working you should get a composite video (plus source silent video and audio) in `/path/to/birdcam/captures` as well as stdout content in the terminal running the process, noting the detection of motion.

## Systemd Service

If you want the motion-detection-and-capture behavior to persist, first update `env` to contain a secure string (uuid, hash, password, whatever), then:

```bash
cp birdcam.service /etc/systemd/system/birdcam.service
mkdir /etc/birdcam && cp env /etc/birdcam/env
sudo systemctl daemon-reload
sudo systemctl enable birdcam.service
sudo systemctl start birdcam.service
sudo systemctl status birdcam.service
journalctl -fu birdcam.service # follow logs
```

## Frontend

Take a look in the `frontend` dir if you want a little view for the livestream as well as your captured clips.

Happy birdcamming!
