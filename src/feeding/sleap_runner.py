"""Run SLEAP inference via its CLI in a separate environment.

For each video we produce, under ``paths.predictions``::

    {video}.slp              raw SLEAP predictions
    {video}.analysis.h5      tracks + point scores (what the pipeline reads)
    {video}.provenance.json  model fingerprint, SLEAP version, command, timings

Outputs are written to temporary names and renamed on success, so an
interrupted run never leaves a half-written file that looks complete.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config, skeleton_from_model
from .naming import parse_video_name
from .poses import node_names
from .provenance import model_fingerprint, now_iso, sha256, write_json
from .settings import sleap_exe
from .video import probe

ProgressFn = Callable[[dict], None]


def prediction_paths(cfg: Config, video: str) -> dict[str, Path]:
    d = cfg.paths.predictions
    return {
        "slp": d / f"{video}.slp",
        "h5": d / f"{video}.analysis.h5",
        "provenance": d / f"{video}.provenance.json",
    }


def has_predictions(cfg: Config, video: str) -> bool:
    return prediction_paths(cfg, video)["h5"].exists()


def models_for(cfg: Config, video: str) -> list[Path]:
    """SLEAP model directories for a video: by its context, else ``default``."""
    return cfg.sleap.model_dirs(parse_video_name(video, cfg.naming.pattern).context)


def inspect_model(bin_dir: Path, model_dir: Path) -> dict:
    """Skeleton (nodes, edges) and head types of a trained SLEAP model.

    Asks SLEAP itself (in its env) to read ``training_config.json``; falls back
    to scanning the JSON for node names when that env is unavailable.
    """
    cfg_path = Path(model_dir) / "training_config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"{model_dir} is not a SLEAP model directory (no training_config.json)")
    code = (
        "import json, sys\n"
        "from sleap.nn.config import TrainingJobConfig\n"
        "c = TrainingJobConfig.load_json(sys.argv[1])\n"
        "sk = c.data.labels.skeletons[0]\n"
        "heads = [k for k, v in vars(c.model.heads).items() if v is not None]\n"
        "print('@@' + json.dumps({'nodes': sk.node_names, 'edges': [list(e) for e in sk.edge_names], 'heads': heads}))\n"
    )
    py = sleap_exe(bin_dir, "python")
    if py is not None:
        out = subprocess.run([str(py), "-c", code, str(cfg_path)], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            if line.startswith("@@"):
                return {**json.loads(line[2:]), "source": "sleap"}
    sk = skeleton_from_model(model_dir)
    return {**sk, "source": "json"}


def sleap_version(cfg: Config) -> str:
    py = sleap_exe(cfg.sleap.bin_dir, "python")
    if py is None:
        return "unknown"
    out = subprocess.run([str(py), "-c", "import sleap; print(sleap.__version__)"],
                         capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def run_inference(
    cfg: Config,
    video_path: Path,
    on_progress: ProgressFn | None = None,
    on_log: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """Run ``sleap-track`` + ``sleap-convert`` for one video; returns the analysis .h5 path."""
    video = video_path.stem
    models = models_for(cfg, video)
    out = prediction_paths(cfg, video)
    out["slp"].parent.mkdir(parents=True, exist_ok=True)
    tmp_slp = out["slp"].with_suffix(".tmp.slp")
    tmp_h5 = out["h5"].with_name(f"{video}.tmp.analysis.h5")
    log = on_log or (lambda s: None)

    track, convert = (sleap_exe(cfg.sleap.bin_dir, n) for n in ("sleap-track", "sleap-convert"))
    if track is None or convert is None:
        raise FileNotFoundError(
            f"SLEAP not found{f' in {cfg.sleap.bin_dir}' if cfg.sleap.bin_dir else ''}: set the SLEAP environment's "
            "bin folder in Settings (sleap.bin_dir), or the FEEDING_SLEAP_BIN environment variable")
    track_cmd = [
        str(track), str(video_path),
        *[a for m in models for a in ("-m", str(m))],
        "--verbosity", "json",
        *cfg.sleap.track_args(),
        "-o", str(tmp_slp),
    ]
    convert_cmd = [str(convert), "--format", "analysis", "-o", str(tmp_h5), str(tmp_slp)]

    started = now_iso()
    t0 = time.time()
    log("$ " + " ".join(track_cmd))
    proc = subprocess.Popen(track_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("{") and "n_processed" in line:
            try:
                msg = json.loads(line)
                if on_progress:
                    on_progress(msg)
                continue
            except json.JSONDecodeError:
                pass
        if line:
            log(line)
        if should_stop and should_stop():
            proc.terminate()
    rc = proc.wait()
    if should_stop and should_stop():
        tmp_slp.unlink(missing_ok=True)
        raise InterruptedError("cancelled")
    if rc != 0:
        tmp_slp.unlink(missing_ok=True)
        raise RuntimeError(f"sleap-track exited with code {rc}")

    log("$ " + " ".join(convert_cmd))
    conv = subprocess.run(convert_cmd, capture_output=True, text=True)
    if conv.returncode != 0:
        log(conv.stdout + conv.stderr)
        raise RuntimeError(f"sleap-convert exited with code {conv.returncode}")

    tmp_slp.replace(out["slp"])
    tmp_h5.replace(out["h5"])
    write_json(out["provenance"], {
        "video": video,
        "video_path": str(video_path),
        "video_sha256": sha256(video_path),
        "source": "sleap-track",
        "model": [model_fingerprint(m) for m in models],
        "sleap_version": sleap_version(cfg),
        "sleap_params": cfg.sleap.params(),
        "commands": [track_cmd, convert_cmd],
        "started_at": started,
        "finished_at": now_iso(),
        "elapsed_s": round(time.time() - t0, 1),
    })
    return out["h5"]


def import_analysis_h5(cfg: Config, src: Path, video_path: Path, expected_nodes: list[str]) -> Path:
    """Adopt an existing SLEAP analysis file (e.g. made elsewhere) for ``video_path``.

    The file is copied (never moved) and checked against the skeleton and the
    video's frame count. Provenance records it as imported, with its hash.
    """
    video = video_path.stem
    nodes = node_names(src)
    if nodes != expected_nodes:
        raise ValueError(f"{src.name}: nodes {nodes} != configured skeleton {expected_nodes}")
    import h5py

    with h5py.File(src, "r") as f:
        T = f["tracks"].shape[-1]
        labels_path = f["labels_path"][()]
    n = probe(video_path).n_frames
    if T > n:
        raise ValueError(f"{src.name}: {T} frames in predictions but video has {n}")
    out = prediction_paths(cfg, video)
    out["h5"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out["h5"])
    write_json(out["provenance"], {
        "video": video,
        "video_path": str(video_path),
        "source": "imported",
        "imported_from": str(src),
        "imported_sha256": sha256(src),
        "original_labels_path": labels_path.decode() if isinstance(labels_path, bytes) else str(labels_path),
        "prediction_frames": T,
        "video_frames": n,
        "imported_at": now_iso(),
    })
    return out["h5"]
