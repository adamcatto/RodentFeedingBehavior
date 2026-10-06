"""Loading SLEAP analysis (.h5) files into per-frame pose tables."""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pandas as pd


def load_analysis_h5(path: Path, n_frames: int | None = None, instance: int = 0) -> pd.DataFrame:
    """Return a frame-indexed table with ``{node}_x``, ``{node}_y``, ``{node}_score``.

    SLEAP stores ``tracks`` as (n_tracks, 2, n_nodes, n_frames) and
    ``point_scores`` as (n_tracks, n_nodes, n_frames). The analysis file stops at
    the last predicted frame, so we pad with NaN up to ``n_frames`` when given.
    Scores of frames without a prediction are NaN.
    """
    with h5py.File(path, "r") as f:
        nodes = [n.decode() if isinstance(n, bytes) else str(n) for n in f["node_names"][:]]
        tracks = np.asarray(f["tracks"][instance], dtype=float)  # (2, n_nodes, T)
        scores = np.asarray(f["point_scores"][instance], dtype=float)  # (n_nodes, T)
    T = tracks.shape[-1]
    n = max(T, n_frames or 0)
    cols: dict[str, np.ndarray] = {}
    for j, node in enumerate(nodes):
        for name, arr in (("x", tracks[0, j]), ("y", tracks[1, j]), ("score", scores[j])):
            col = np.full(n, np.nan)
            col[:T] = arr
            cols[f"{node}_{name}"] = col
    df = pd.DataFrame(cols)
    df.index.name = "frame"
    # A frame with no instance has NaN coordinates; make its scores NaN too.
    for node in nodes:
        df.loc[df[f"{node}_x"].isna(), f"{node}_score"] = np.nan
    return df


def node_names(path: Path) -> list[str]:
    with h5py.File(path, "r") as f:
        return [n.decode() if isinstance(n, bytes) else str(n) for n in f["node_names"][:]]
