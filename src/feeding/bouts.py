"""Feeding-bout detection from per-frame bowl contact.

A *bout* is a cluster of bowl-contact episodes::

    ... away ... | approach | contact ~ short away ~ contact ... | withdrawal | ... away ...
                 ^ window_start  ^ contact_start            contact_end ^      window_end ^

1. Contact episodes are runs of ``in_bowl`` frames inside one tracked segment.
2. Episodes separated by fewer than ``merge_gap_frames`` away-frames are merged
   into one bout; each extra episode counts as a "return" to the food.
3. A bout is kept if at least one of its episodes lasts ``min_contact_frames``
   consecutive frames (filters out snout positions that only graze the rim).
4. A bout needs ``flank_frames`` of tracked, contact-free frames on both sides,
   used as its approach and withdrawal windows.

Each bout is emitted exactly once, however many qualifying episodes it contains.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import BoutConfig
from .tracks import runs

BOUT_COLUMNS = [
    "bout", "segment", "window_start", "contact_start", "contact_end", "window_end",
    "n_episodes", "contact_frames", "longest_episode",
]


def detect_bouts(tracks: pd.DataFrame, cfg: BoutConfig) -> pd.DataFrame:
    in_bowl = tracks["in_bowl"].to_numpy(bool)
    segment = tracks["segment"].to_numpy()
    rows = []
    for k in np.unique(segment[segment >= 0]):
        idx = np.flatnonzero(segment == k)
        s0, s1 = int(idx[0]), int(idx[-1]) + 1  # segments are contiguous
        episodes = [(s0 + a, s0 + b) for a, b in runs(in_bowl[s0:s1])]
        if not episodes:
            continue
        groups: list[list[tuple[int, int]]] = [[episodes[0]]]
        for ep in episodes[1:]:
            if ep[0] - groups[-1][-1][1] < cfg.merge_gap_frames:
                groups[-1].append(ep)
            else:
                groups.append([ep])
        for g in groups:
            longest = max(b - a for a, b in g)
            if longest < cfg.min_contact_frames:
                continue
            cs, ce = g[0][0], g[-1][1]
            ws, we = cs - cfg.flank_frames, ce + cfg.flank_frames
            if ws < s0 or we > s1:
                continue
            # Flanks must be free of contact (possible when flank > merge gap).
            if in_bowl[ws:cs].any() or in_bowl[ce:we].any():
                continue
            rows.append({
                "segment": int(k), "window_start": ws, "contact_start": cs, "contact_end": ce,
                "window_end": we, "n_episodes": len(g),
                "contact_frames": int(sum(b - a for a, b in g)), "longest_episode": int(longest),
            })
    df = pd.DataFrame(rows, columns=[c for c in BOUT_COLUMNS if c != "bout"])
    df.insert(0, "bout", np.arange(len(df)))
    return df
