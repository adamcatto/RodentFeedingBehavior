"""Per-bout and per-session behavioural features.

Bout features:

- ``approach_indirectness`` / ``withdrawal_indirectness``: standard deviation (deg)
  of the angle between each frame-to-frame step of the bout node and the
  direction to the bowl centre, over the approach / withdrawal window.
- ``num_returns``: contact episodes in the bout minus one.
- ``num_interaction_frames``: frames from first to last contact.
- ``{node}_{phase}_speed_mean|std``: step length (px/frame) per phase.

Additions: contact frames / fraction, mean approach heading, durations in seconds.

Session measures (``session_summary``) add whole-video locomotion of
``features.body_node`` (a body-centre keypoint such as MidBack by default) over valid frames: distance
travelled, speed, time moving and distance kept from the bowl.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def heading_angles(x: np.ndarray, y: np.ndarray, cx: float, cy: float) -> np.ndarray:
    """Angle (deg, 0..180) between each step p[i]->p[i+1] and the vector p[i]->bowl.

    0 deg = moving straight at the bowl, 180 deg = straight away. Zero-length or
    NaN steps are dropped.
    """
    step = np.column_stack([np.diff(x), np.diff(y)])
    to_bowl = np.column_stack([cx - x[:-1], cy - y[:-1]])
    n1, n2 = np.linalg.norm(step, axis=1), np.linalg.norm(to_bowl, axis=1)
    ok = (n1 > 0) & (n2 > 0) & np.isfinite(n1) & np.isfinite(n2)
    cos = np.einsum("ij,ij->i", step[ok], to_bowl[ok]) / (n1[ok] * n2[ok])
    return np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))


def step_lengths(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.hypot(np.diff(x), np.diff(y))


def _nan_stat(fn, a: np.ndarray) -> float:
    a = a[np.isfinite(a)]
    if len(a) < (2 if fn is np.std else 1):
        return np.nan
    return float(fn(a, ddof=1)) if fn is np.std else float(fn(a))


def bout_features(tracks: pd.DataFrame, bouts: pd.DataFrame, cx: float, cy: float, fps: float, cfg: Config) -> pd.DataFrame:
    node = cfg.bouts.node
    X = {n: tracks[f"{n}_x"].to_numpy() for n in cfg.features.speed_nodes + [node]}
    Y = {n: tracks[f"{n}_y"].to_numpy() for n in cfg.features.speed_nodes + [node]}
    rows = []
    for b in bouts.itertuples(index=False):
        phases = {
            "approach": slice(b.window_start, b.contact_start),
            "interaction": slice(b.contact_start, b.contact_end),
            "withdrawal": slice(b.contact_end, b.window_end),
        }
        appr = heading_angles(X[node][phases["approach"]], Y[node][phases["approach"]], cx, cy)
        withd = heading_angles(X[node][phases["withdrawal"]], Y[node][phases["withdrawal"]], cx, cy)
        span = b.contact_end - b.contact_start
        f = {
            "bout": b.bout,
            "start_frame": b.window_start,
            "approach_indirectness": _nan_stat(np.std, appr),
            "withdrawal_indirectness": _nan_stat(np.std, withd),
            "approach_heading_mean": _nan_stat(np.mean, appr),
            "num_returns": b.n_episodes - 1,
            "num_interaction_frames": span,
            "interaction_duration_s": span / fps,
            "contact_frames": b.contact_frames,
            "contact_fraction": b.contact_frames / span,
        }
        for n in cfg.features.speed_nodes:
            for phase, sl in phases.items():
                v = step_lengths(X[n][sl], Y[n][sl])
                f[f"{n}_{phase}_speed_mean"] = _nan_stat(np.mean, v)
                f[f"{n}_{phase}_speed_std"] = _nan_stat(np.std, v)
        rows.append(f)
    return pd.DataFrame(rows)


def locomotion(tracks: pd.DataFrame, node: str, cx: float, cy: float, fps: float, moving_speed: float) -> dict:
    """Whole-session locomotion of ``node`` over valid frames.

    Positions are smoothed with a centred 5-frame mean (within each valid
    segment) before differencing, so tracking jitter does not count as movement.
    Steps between segments are not counted.
    """
    valid = tracks["valid"].to_numpy(bool)
    seg = tracks["segment"].to_numpy() if "segment" in tracks else np.where(valid, 0, -1)
    x = tracks[f"{node}_x"].where(valid)
    y = tracks[f"{node}_y"].where(valid)
    grp = pd.Series(seg, index=tracks.index)
    xs = x.groupby(grp).transform(lambda s: s.rolling(5, center=True, min_periods=1).mean()).to_numpy()
    ys = y.groupby(grp).transform(lambda s: s.rolling(5, center=True, min_periods=1).mean()).to_numpy()
    step = np.hypot(np.diff(xs), np.diff(ys))
    same = (seg[1:] == seg[:-1]) & (seg[1:] >= 0)
    step = step[same & np.isfinite(step)]
    dist = np.hypot(x.to_numpy() - cx, y.to_numpy() - cy)
    n = f"{node}_"
    if not len(step):
        return {n + k: np.nan for k in ("distance_px", "speed_mean", "speed_p90", "moving_fraction", "moving_speed_mean", "bowl_distance_mean")}
    moving = step > moving_speed
    return {
        n + "distance_px": float(step.sum()),
        n + "speed_mean": float(step.mean()),
        n + "speed_p90": float(np.quantile(step, 0.9)),
        n + "moving_fraction": float(moving.mean()),
        n + "moving_speed_mean": float(step[moving].mean()) if moving.any() else np.nan,
        n + "bowl_distance_mean": float(np.nanmean(dist)) if np.isfinite(dist).any() else np.nan,
    }


def session_summary(tracks: pd.DataFrame, bouts: pd.DataFrame, fps: float, loco: dict | None = None) -> dict:
    """Whole-video measures that do not depend on bout segmentation details."""
    valid = tracks["valid"].to_numpy(bool)
    in_bowl = tracks["in_bowl"].to_numpy(bool)
    first = int(np.argmax(in_bowl)) if in_bowl.any() else None
    return {
        "n_frames": len(tracks),
        "valid_frames": int(valid.sum()),
        "valid_fraction": float(valid.mean()) if len(valid) else np.nan,
        "in_bowl_frames": int(in_bowl.sum()),
        "in_bowl_fraction_of_valid": float(in_bowl.sum() / valid.sum()) if valid.any() else np.nan,
        "in_bowl_time_s": float(in_bowl.sum() / fps),
        "latency_to_first_contact_s": first / fps if first is not None else np.nan,
        "n_bouts": len(bouts),
        "bouts_per_valid_min": len(bouts) / (valid.sum() / fps / 60) if valid.any() else np.nan,
        "mean_bout_frames": float((bouts["contact_end"] - bouts["contact_start"]).mean()) if len(bouts) else np.nan,
        **(loco or {}),
    }
