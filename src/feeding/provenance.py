"""Provenance: file hashes, git state and package versions recorded with outputs."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from .config import PROJECT_ROOT

_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of a file, memoised on (path, size, mtime)."""
    st = Path(path).stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _HASH_CACHE:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while b := f.read(chunk):
                h.update(b)
        _HASH_CACHE[key] = h.hexdigest()
    return _HASH_CACHE[key]


def model_fingerprint(model_dir: Path) -> dict:
    """Identify a SLEAP model by hashing its weights and training config."""
    model_dir = Path(model_dir)
    return {
        "path": str(model_dir),
        "best_model_sha256": sha256(model_dir / "best_model.h5"),
        "training_config_sha256": sha256(model_dir / "training_config.json"),
    }


def git_state(root: Path = PROJECT_ROOT) -> dict:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()

    try:
        commit = run("rev-parse", "HEAD") or None
        dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
    except OSError:
        commit, dirty = None, None
    return {"commit": commit, "dirty": dirty}


def package_versions() -> dict:
    pkgs = ["feeding", "numpy", "pandas", "scipy", "scikit-learn", "h5py", "opencv-python-headless",
            "matplotlib", "seaborn", "umap-learn", "torch"]
    out = {"python": platform.python_version(), "platform": platform.platform()}
    for p in pkgs:
        try:
            out[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            pass
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n")
