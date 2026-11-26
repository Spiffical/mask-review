"""Video for frames cut from a clip (optional; needs ffmpeg and ffprobe on the PATH).

Most reviews are of still images and need none of this. When frames come from video, the page can play each frame's
clip from the labelled moment, over the frame, and a reviewer can make a new frame from another moment of it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def rate(video: Path) -> float:
    """The video's nominal frame rate (some clips drop frames, so the average rate would step too far)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0",
                        str(video)], capture_output=True, text=True, check=True).stdout.strip().strip(",").split("/")
    return round(float(r[0]) / float(r[1] if len(r) > 1 else 1), 4)


def tag_colours(video: Path):
    """Say BT.601 in a clip that names no colour matrix: ffmpeg reads such clips as BT.601 when it cuts stills, while a
    browser takes untagged HD video for BT.709, so the picture would change colour when the video starts."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=color_space", "-of", "csv=p=0",
                        str(video)], capture_output=True, text=True, check=True).stdout.strip().strip(",")
    if r not in ("", "unknown"):
        return
    part = video.with_suffix(".part.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-c", "copy", "-bsf:v",
                    "h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=6:video_full_range_flag=0",
                    "-movflags", "+faststart", str(part)], check=True)
    part.replace(video)


def prepare(src: Path, dst: Path, W: int, H: int) -> float:
    """The clip re-encoded at the frame's size with a key frame every second (so it can be scrubbed); returns its frame
    rate, or 0 when there is no clip to use."""
    if not src.exists() or not have_ffmpeg():
        return 0.0
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        fps = rate(src)
        part = dst.with_suffix(".part.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-an", "-vf", f"scale={W}:{H}", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "27", "-maxrate", "4500k", "-bufsize", "9M", "-pix_fmt", "yuv420p",
                        "-g", str(max(2, round(fps))), "-movflags", "+faststart", str(part)], check=True)
        part.replace(dst)
        tag_colours(dst)
    return rate(dst)


def find_still(img: np.ndarray, video: Path, about: float):
    """When the still appears in the video: of the frames within a second of the nominal time, the one whose picture is
    closest to it (clips that drop frames put the nominal time off by a few frames). Returns (time, shift, difference)."""
    import cv2
    w, h = 320, 180
    want = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    t0 = max(0.0, about - 1.0)
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{t0:.3f}", "-t", "2.2", "-copyts", "-i", str(video), "-vsync", "0",
                        "-vf", f"scale={w}:{h}:flags=bilinear,format=gray,showinfo", "-f", "rawvideo", "-"], capture_output=True)
    times = [float(x) for x in re.findall(r"pts_time:\s*([0-9.]+)", p.stderr.decode(errors="replace"))]
    frames = np.frombuffer(p.stdout, np.uint8)
    frames = frames[:len(frames) // (w * h) * w * h].reshape(-1, h, w)
    if not len(frames) or len(frames) != len(times):
        return about, 0.0, None
    diffs = [float(np.abs(f.astype(np.float32) - want).mean()) for f in frames]
    # In a still scene many frames fit equally well: of those within 10% of the best, the one nearest the nominal time.
    k = min((i for i, d in enumerate(diffs) if d <= 1.1 * min(diffs)), key=lambda i: abs(times[i] - about))
    return round(times[k], 4), times[k] - about, round(diffs[k], 2)
