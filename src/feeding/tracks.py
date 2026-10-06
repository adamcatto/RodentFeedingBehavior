"""Per-video preprocessing: quality filtering and per-frame bowl contact.

The result for each video is a frame-indexed table (cached as parquet under
``paths.derived``) with columns::

    {node}_x, {node}_y, {node}_score   SLEAP output (all skeleton nodes)
    valid                              frame passes the confidence filter
    segment                            id of the contiguous valid stretch (-1 if invalid)
    bowl_dist                          px from bout node to the clicked bowl centre
    rim_dist                           signed px from bout node to bowl rim (<0 inside; ellipse or polygon)
    in_bowl                            valid & rim_dist <= bouts.margin_px

The cache is keyed on the hashes of the predictions file, the bowl annotation
and the analysis parameters, so editing any of them invalidates it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .bowl import BowlAnnotation, bowl_path, load_bowl
from .config import Config
from .poses import load_analysis_h5
from .provenance import sha256
from .sleap_runner import prediction_paths
from .video import probe


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Half-open [start, end) intervals where ``mask`` is True."""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]])
    d = np.flatnonzero(np.diff(m.astype(np.int8)))
    return list(zip(d[::2].tolist(), d[1::2].tolist()))


def quality_mask(poses: pd.DataFrame, nodes: list[str], window: int, min_score: float) -> np.ndarray:
    """Frames whose centred rolling-mean score is >= ``min_score`` for every node.

    Missing scores count as 0, and the first/last ``window // 2`` frames (where
    the rolling window is incomplete) are invalid.
    """
    scores = poses[[f"{n}_score" for n in nodes]].fillna(0.0)
    rolled = scores.rolling(window=window, center=True).mean()
    return (rolled.min(axis=1, skipna=False) >= min_score).to_numpy()


def segment_ids(valid: np.ndarray, min_len: int) -> np.ndarray:
    seg = np.full(len(valid), -1, dtype=np.int32)
    k = 0
    for s, e in runs(valid):
        if e - s >= min_len:
            seg[s:e] = k
            k += 1
    return seg


def interpolate_gaps(df: pd.DataFrame, cols: list[str], segments: np.ndarray, max_gap: int) -> pd.DataFrame:
    """Linearly fill NaN gaps of <= ``max_gap`` frames, never across segment boundaries."""
    if max_gap <= 0:
        return df
    df = df.copy()
    for k in np.unique(segments[segments >= 0]):
        idx = np.flatnonzero(segments == k)
        sub = df.iloc[idx][cols]
        df.iloc[idx, [df.columns.get_loc(c) for c in cols]] = sub.interpolate(
            limit=max_gap, limit_area="inside"
        ).to_numpy()
    return df


def compute_tracks(poses: pd.DataFrame, ann: BowlAnnotation, cfg: Config) -> pd.DataFrame:
    q, b = cfg.quality, cfg.bouts
    nodes = cfg.tracked_nodes
    valid = quality_mask(poses, nodes, q.score_window, q.min_score)
    seg = segment_ids(valid, q.min_segment_frames)
    valid = seg >= 0
    xy_cols = [f"{n}_{c}" for n in nodes for c in ("x", "y")]
    df = interpolate_gaps(poses, xy_cols, seg, q.max_interp_gap)
    df["valid"] = valid
    df["segment"] = seg
    x, y = df[f"{b.node}_x"].to_numpy(), df[f"{b.node}_y"].to_numpy()
    df["bowl_dist"] = np.hypot(x - ann.center[0], y - ann.center[1])
    df["rim_dist"] = ann.rim().rim_distance(x, y)
    with np.errstate(invalid="ignore"):
        df["in_bowl"] = valid & (df["rim_dist"].to_numpy() <= b.margin_px)
    return df


# --------------------------------------------------------------------------- caching


def _cache_key(cfg: Config, video: str) -> dict:
    preds = prediction_paths(cfg, video)["h5"]
    bp = bowl_path(cfg.paths.bowls, video)
    ann = BowlAnnotation.model_validate_json(bp.read_text(encoding="utf-8"))
    geom = ann.geometry()
    return {
        "predictions_sha256": sha256(preds),
        "bowl_sha256": hashlib.sha256(json.dumps(geom, sort_keys=True).encode()).hexdigest(),
        "analysis_hash": cfg.analysis_hash(),
    }


def derived_paths(cfg: Config, video: str) -> tuple[Path, Path]:
    d = cfg.paths.derived / "tracks"
    return d / f"{video}.parquet", d / f"{video}.json"


def missing_inputs(cfg: Config, video: str) -> list[str]:
    out = []
    if not prediction_paths(cfg, video)["h5"].exists():
        out.append("predictions")
    if not bowl_path(cfg.paths.bowls, video).exists():
        out.append("bowl")
    return out


def load_tracks(cfg: Config, video: str, video_path: Path, use_cache: bool = True) -> tuple[pd.DataFrame, dict]:
    """Return (tracks table, info) for a video, recomputing if inputs changed.

    ``info`` holds the cache key plus fps, frame count, bowl centre and rim geometry.
    """
    missing = missing_inputs(cfg, video)
    if missing:
        raise FileNotFoundError(f"{video}: missing {', '.join(missing)}")
    table_p, info_p = derived_paths(cfg, video)
    key = _cache_key(cfg, video)
    if use_cache and table_p.exists() and info_p.exists():
        info = json.loads(info_p.read_text(encoding="utf-8"))
        if info.get("key") == key:
            return pd.read_parquet(table_p), info

    props = probe(video_path)
    ann = load_bowl(cfg.paths.bowls, video)
    assert ann is not None
    poses = load_analysis_h5(prediction_paths(cfg, video)["h5"], n_frames=props.n_frames)
    df = compute_tracks(poses, ann, cfg)
    info = {
        "key": key,
        "video": video,
        "fps": props.fps,
        "n_frames": len(df),
        "width": props.width,
        "height": props.height,
        "center": list(ann.center),
        "rim": ann.rim().to_dict(),
        "valid_frames": int(df["valid"].sum()),
        "in_bowl_frames": int(df["in_bowl"].sum()),
    }
    table_p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(table_p)
    info_p.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return df, info
