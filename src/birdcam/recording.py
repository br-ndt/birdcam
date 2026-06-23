"""Clip-side helpers shared by both engines: finalising a completed clip (thumbnail +
atomic rename), thumbnail generation, disk retention, and clip listing/validation. The
mechanism that *produces* a clip differs per engine (the standalone muxes camera H.264 +
mic audio; the recorder writes the node's stream straight to disk), but everything from a
finished mp4 onward is identical and lives here."""
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from birdcam.config import CAPTURE_DIR, THUMB_DIR, THUMB_WIDTH, RETENTION_TARGET_MB


def _cleanup(*paths):
    for p in paths:
        try:
            Path(p).unlink()
        except OSError:
            pass


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


def free_mb():
    return shutil.disk_usage(CAPTURE_DIR).free / (1024 * 1024)


def enforce_retention():
    """Delete oldest clips (+ thumbnails) until free space is back above the target. Returns
    the resulting free MB. A no-op when there's already room."""
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


def finalize_clip(working_path, final_path):
    """Given a COMPLETE mp4 at working_path, build its thumbnail and atomically rename into
    place so the UI only ever sees a finished, playable file. Discards an empty/missing
    working file rather than publishing a broken clip."""
    if not working_path or not final_path:
        return
    wp = Path(working_path)
    if not wp.exists() or wp.stat().st_size == 0:
        print(f"WARNING: no video for {Path(final_path).name}; discarding", flush=True)
        _cleanup(working_path)
        return
    generate_thumbnail(working_path, THUMB_DIR / (Path(final_path).stem + ".jpg"))
    try:
        os.rename(working_path, final_path)
        print(f"saved -> {Path(final_path).name}", flush=True)
    except OSError as e:
        print(f"WARNING: finalize failed for {Path(final_path).name}: {e!r}", flush=True)
        _cleanup(working_path)


def is_valid_clip_name(filename):
    """Reject anything that isn't a final clip, including path traversal."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    if not filename.startswith("clip_") or not filename.endswith(".mp4"):
        return False
    return True


def list_clips(page=1, per_page=10):
    """Return (clips_for_page, total_count) as (name, datetime, size_mb), newest first."""
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
