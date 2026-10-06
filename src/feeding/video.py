"""Video probing, frame access, and (optional) quadrant splitting."""

from __future__ import annotations

import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class VideoProps:
    width: int
    height: int
    fps: float
    n_frames: int


def probe(path: Path) -> VideoProps:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise OSError(f"Cannot open video {path}")
    try:
        return VideoProps(
            width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            fps=float(cap.get(cv2.CAP_PROP_FPS)),
            n_frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        )
    finally:
        cap.release()


class FrameReader:
    """Thread-safe random access to frames, keeping a few captures open.

    Sequential reads (the common case while playing back) avoid a seek.
    """

    def __init__(self, max_open: int = 4):
        self._caps: OrderedDict[Path, tuple[cv2.VideoCapture, int]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_open = max_open

    def read(self, path: Path, idx: int) -> np.ndarray:
        path = Path(path)
        with self._lock:
            cap, next_idx = self._caps.pop(path, (None, -1))
            if cap is None:
                cap = cv2.VideoCapture(str(path))
                if not cap.isOpened():
                    raise OSError(f"Cannot open video {path}")
            if idx != next_idx:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            self._caps[path] = (cap, idx + 1 if ok else -1)
            while len(self._caps) > self._max_open:
                _, (old, _) = self._caps.popitem(last=False)
                old.release()
        if not ok:
            raise IndexError(f"Frame {idx} unreadable in {path.name}")
        return frame

    def close(self, path: Path | None = None) -> None:
        """Release one video (or all). Windows cannot replace, move or delete a file that is open."""
        with self._lock:
            for p in [Path(path)] if path is not None else list(self._caps):
                cap, _ = self._caps.pop(p, (None, -1))
                if cap is not None:
                    cap.release()

    def jpeg(self, path: Path, idx: int, quality: int = 85) -> bytes:
        ok, buf = cv2.imencode(".jpg", self.read(path, idx), [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return buf.tobytes()


# --------------------------------------------------------------------------- splitting


def quadrant_outputs(raw: Path, out_dir: Path) -> list[Path]:
    """``{prefix}_{TL}_{TR}_{BL}_{BR}.<ext>`` -> four ``{prefix}_{animal}.mp4`` paths (TL, TR, BL, BR).

    The last four ``_``-separated parts of the name are the animal IDs in the top-left,
    top-right, bottom-left and bottom-right arenas; the rest is kept as a prefix
    (e.g. ``3_Post_A``)."""
    parts = raw.stem.split("_")
    base, animals = "_".join(parts[:-4]), parts[-4:]
    if len(animals) != 4 or not all(a.isalnum() for a in animals) or not base:
        raise ValueError(f"{raw.name}: expected {{prefix}}_{{TL}}_{{TR}}_{{BL}}_{{BR}}")
    return [out_dir / f"{base}_{a}.mp4" for a in animals]


def split_quadrants(raw: Path, out_dir: Path, crf: int = 18, gop: int = 30, overwrite: bool = False) -> list[Path]:
    """Crop a 2x2 recording into four per-animal videos in one ffmpeg pass.

    Keeps the exact source frame rate (e.g. 29.97) and encodes H.264 with a
    short keyframe interval so that frame-indexed seeking (SLEAP, the UI) is
    exact and fast.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = quadrant_outputs(raw, out_dir)
    if not overwrite and all(o.exists() for o in outs):
        return outs
    crops = ["crop=iw/2:ih/2:0:0", "crop=iw/2:ih/2:iw/2:0", "crop=iw/2:ih/2:0:ih/2", "crop=iw/2:ih/2:iw/2:ih/2"]
    graph = ";".join(f"[0:v]{c}[q{i}]" for i, c in enumerate(crops))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-filter_complex", graph]
    for i, o in enumerate(outs):
        cmd += ["-map", f"[q{i}]", "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
                "-g", str(gop), "-pix_fmt", "yuv420p", "-an", str(o)]
    subprocess.run(cmd, check=True)
    return outs
