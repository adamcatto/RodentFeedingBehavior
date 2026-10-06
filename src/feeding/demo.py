"""A synthetic demo project: rendered videos of a simulated mouse plus SLEAP-format predictions.

``feeding demo [DIR]`` (or *Try the demo project* in the UI) creates a complete
project that runs without SLEAP or real data, to try the app or to test an
installation:

- two groups (``Control``, ``Treated``) of ``animals_per_group`` animals,
- four sessions per animal: ``Pre`` and ``Post`` in a square (``A``) and a round
  (``B``) arena, named ``{session}_{condition}_{context}_{animal}``,
- a six-node skeleton (Snout, LeftEar, RightEar, MidBack, TailBase, TailTip)
  with realistic confidence scores, a low-confidence tail tip and a few
  tracking dropouts,
- bowl annotations (optional), ``metadata/subjects.csv`` and ``feeding.yaml``.

The simulated animal explores, approaches the bowl, feeds (sometimes stepping
back and returning), and withdraws. Treated animals in ``Post`` sessions visit
the bowl more often, feed longer, return more and move more slowly, so the
comparisons have something to find. Everything is seeded and reproducible.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

NODES = ["Snout", "LeftEar", "RightEar", "MidBack", "TailBase", "TailTip"]
EDGES = [["Snout", "LeftEar"], ["Snout", "RightEar"], ["LeftEar", "MidBack"], ["RightEar", "MidBack"],
         ["MidBack", "TailBase"], ["TailBase", "TailTip"]]
W, H, FPS = 320, 240, 30.0
SESSIONS = [("1", "Pre", "A"), ("2", "Pre", "B"), ("3", "Post", "A"), ("4", "Post", "B")]
ARENAS = {  # context -> shape, geometry, bowl (cx, cy, r)
    "A": {"shape": "square", "box": (40, 20, 280, 220), "bowl": (238.0, 64.0, 13.0)},
    "B": {"shape": "round", "circle": (160, 120, 106), "bowl": (100.0, 158.0, 13.0)},
}
CHAMBERS = {"A": "square arena", "B": "round arena"}


def _behaviour(group: str, condition: str, rng: np.random.Generator) -> dict:
    """Behavioural parameters of one animal-session (with per-animal variation)."""
    treated_post = group == "Treated" and condition == "Post"
    b = {
        "visit_interval": 200 if treated_post else 300,     # mean frames between bowl visits
        "feed_frames": 120 if treated_post else 65,          # mean contact duration
        "p_return": 0.6 if treated_post else 0.3,            # chance to step back and return
        "explore_speed": 1.5 if treated_post else 2.3,       # px / frame
        "approach_speed": 2.0 if treated_post else 3.0,
        "wobble": 0.55 if treated_post else 0.25,            # approach indirectness
    }
    if condition == "Post":
        b["explore_speed"] *= 0.9  # everyone habituates a little
    return {k: v * float(np.exp(rng.normal(0, 0.08))) for k, v in b.items()}


def _inside(ctx: str, p: np.ndarray, margin: float = 0.0) -> np.ndarray:
    a = ARENAS[ctx]
    if a["shape"] == "square":
        x0, y0, x1, y1 = a["box"]
        return np.clip(p, (x0 + margin, y0 + margin), (x1 - margin, y1 - margin))
    cx, cy, r = a["circle"]
    d = p - (cx, cy)
    n = np.hypot(*d)
    return p if n <= r - margin else np.array([cx, cy]) + d / n * (r - margin)


def _random_point(ctx: str, rng: np.random.Generator, away_from=None, min_dist: float = 0.0) -> np.ndarray:
    a = ARENAS[ctx]
    for _ in range(100):
        if a["shape"] == "square":
            x0, y0, x1, y1 = a["box"]
            p = rng.uniform((x0 + 25, y0 + 25), (x1 - 25, y1 - 25))
        else:
            cx, cy, r = a["circle"]
            ang, rad = rng.uniform(0, 2 * np.pi), (r - 25) * np.sqrt(rng.uniform())
            p = np.array([cx + rad * np.cos(ang), cy + rad * np.sin(ang)])
        if away_from is None or np.hypot(*(p - away_from)) >= min_dist:
            return p
    return p


def simulate(ctx: str, b: dict, n_frames: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """True keypoints, "predicted" keypoints (noise, dropouts) -- both (n_frames, n_nodes, 2) --
    and their scores (n_frames, n_nodes) for one session."""
    bx, by, _ = ARENAS[ctx]["bowl"]
    bowl = np.array([bx, by])
    pos = _random_point(ctx, rng, bowl, 80)
    heading = rng.uniform(0, 2 * np.pi)
    state, target, timer = "explore", _random_point(ctx, rng), 0
    next_visit = int(rng.exponential(b["visit_interval"]) + 120)
    returns, centre, head = 0, np.zeros((n_frames, 2)), np.zeros(n_frames)
    for t in range(n_frames):
        to_bowl = bowl - pos
        dist = np.hypot(*to_bowl)
        feed_pos = bowl - to_bowl / max(dist, 1e-6) * 12.0  # body centre with the snout over the bowl
        if state == "explore":
            next_visit -= 1
            if np.hypot(*(target - pos)) < 4:
                target = _random_point(ctx, rng)
            step = target - pos
            speed = b["explore_speed"] * (0.6 + 0.8 * rng.random())
            if next_visit <= 0:
                state, returns = "approach", 0
        elif state == "approach":
            step = feed_pos - pos
            n = np.hypot(*step)
            if n < 2.0:
                state, timer = "feed", max(20, int(rng.gamma(4, b["feed_frames"] / 4)))
                step, speed = np.zeros(2), 0.0
            else:
                perp = np.array([-step[1], step[0]]) / n
                step = step / n + perp * b["wobble"] * np.sin(t / 6.0)
                speed = min(n, b["approach_speed"] * (0.8 + 0.4 * rng.random()))
        elif state == "feed":
            timer -= 1
            step, speed = rng.normal(0, 1, 2), 0.25
            if timer <= 0:
                if returns < 3 and rng.random() < b["p_return"]:
                    state, timer = "step_back", int(rng.integers(20, 50))
                    target = _inside(ctx, bowl - to_bowl / max(dist, 1e-6) * 34.0, 15)
                    returns += 1
                else:
                    state, target = "withdraw", _random_point(ctx, rng, bowl, 90)
        elif state == "step_back":
            timer -= 1
            step, speed = target - pos, 1.2
            if timer <= 0:
                state = "approach"
        else:  # withdraw
            step = target - pos
            speed = b["approach_speed"]
            if np.hypot(*step) < 4:
                state, target = "explore", _random_point(ctx, rng)
                next_visit = int(rng.exponential(b["visit_interval"]) + 150)
        n = np.hypot(*step)
        if n > 1e-6 and speed > 0:
            pos = _inside(ctx, pos + step / n * min(speed, max(n, 0.25)), 14)
            if state != "feed" and speed > 0.4:
                want = np.arctan2(step[1], step[0])
                heading += 0.35 * np.angle(np.exp(1j * (want - heading)))
        if state == "feed":
            want = np.arctan2(to_bowl[1], to_bowl[0]) + 0.25 * np.sin(t / 9.0)
            heading += 0.3 * np.angle(np.exp(1j * (want - heading)))
        centre[t], head[t] = pos, heading
    u = np.column_stack([np.cos(head), np.sin(head)])
    v = np.column_stack([-u[:, 1], u[:, 0]])
    wig = 0.5 * np.sin(np.arange(n_frames) / 8.0)
    tail_dir = np.column_stack([np.cos(head + wig), np.sin(head + wig)])
    tail_base = centre - 13 * u
    pts = np.stack([centre + 15 * u, centre + 8 * u + 5 * v, centre + 8 * u - 5 * v, centre, tail_base,
                    tail_base - 16 * tail_dir], axis=1)
    true = pts.copy()
    pts = pts + rng.normal(0, 0.4, pts.shape)
    scores = np.clip(rng.normal(0.9, 0.04, (n_frames, len(NODES))), 0, 1)
    scores[:, NODES.index("TailTip")] = np.clip(rng.normal(0.45, 0.15, n_frames), 0, 1)
    for _ in range(2):  # tracking dropouts: occluded / lost animal
        s = int(rng.integers(0, n_frames - 80))
        e = s + int(rng.integers(20, 70))
        if rng.random() < 0.5:
            pts[s:e] = np.nan
            scores[s:e] = np.nan
        else:
            scores[s:e] *= 0.25
            pts[s:e] += rng.normal(0, 6, pts[s:e].shape)
    return true, pts, scores


def _background(ctx: str, rng: np.random.Generator) -> np.ndarray:
    import cv2

    noise = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 3)
    img = np.clip(70 + 6 * noise, 0, 255)[..., None].repeat(3, 2).astype(np.uint8)
    a = ARENAS[ctx]
    floor = np.clip(196 + 5 * noise, 0, 255).astype(np.uint8)
    mask = np.zeros((H, W), np.uint8)
    if a["shape"] == "square":
        x0, y0, x1, y1 = a["box"]
        cv2.rectangle(mask, (x0, y0), (x1, y1), 255, -1)
    else:
        cx, cy, r = a["circle"]
        cv2.circle(mask, (cx, cy), r, 255, -1, cv2.LINE_AA)
    img[mask > 0] = floor[mask > 0][:, None].repeat(3, 1) - np.array([0, 2, 8], np.uint8)
    if a["shape"] == "square":
        cv2.rectangle(img, (x0, y0), (x1, y1), (95, 95, 100), 5)
    else:
        cv2.circle(img, (cx, cy), r, (95, 95, 100), 5, cv2.LINE_AA)
    bx, by, br = a["bowl"]
    cv2.circle(img, (int(bx), int(by)), int(br), (150, 160, 170), -1, cv2.LINE_AA)
    cv2.circle(img, (int(bx), int(by)), int(br), (80, 85, 95), 2, cv2.LINE_AA)
    for _ in range(18):  # food pellets
        ang, rad = rng.uniform(0, 2 * np.pi), (br - 5) * np.sqrt(rng.uniform())
        cv2.circle(img, (int(bx + rad * np.cos(ang)), int(by + rad * np.sin(ang))), 2, (60, 100, 140), -1, cv2.LINE_AA)
    return img


def render(path: Path, ctx: str, pts: np.ndarray, rng: np.random.Generator) -> None:
    """Draw the simulated animal (from its true keypoints) over the arena."""
    import cv2

    bg = _background(ctx, rng)
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for snout, le, re, mid, tb, tt in pts:
        img = bg.copy()
        ip = lambda p: (int(round(p[0])), int(round(p[1])))  # noqa: E731
        cv2.line(img, ip(tb), ip(tt), (120, 125, 150), 2, cv2.LINE_AA)
        axis = snout - tb
        ang = float(np.degrees(np.arctan2(axis[1], axis[0])))
        body_c = tb + 0.42 * axis
        cv2.ellipse(img, ip(body_c), (15, 8), ang, 0, 360, (55, 58, 66), -1, cv2.LINE_AA)
        cv2.ellipse(img, ip(mid + 0.55 * (snout - mid)), (8, 6), ang, 0, 360, (62, 66, 75), -1, cv2.LINE_AA)
        for e in (le, re):
            cv2.circle(img, ip(e), 3, (140, 140, 190), -1, cv2.LINE_AA)
        cv2.circle(img, ip(snout), 2, (150, 150, 210), -1, cv2.LINE_AA)
        vw.write(img)
    vw.release()


def make_demo(root: Path, animals_per_group: int = 5, seconds: float = 120.0, seed: int = 0, bowls: bool = True,
              force: bool = False, log: Callable[[str], None] = print,
              progress: Callable[[int, int], None] | None = None) -> Path:
    """Create the demo project in ``root``; returns its feeding.yaml path."""
    import h5py

    from .bowl import BowlAnnotation, save_bowl
    from .config import load_project
    from .project import init_project, update_project
    from .provenance import now_iso, write_json

    if seconds < 5 or animals_per_group < 1:
        raise ValueError("the demo needs videos of at least 5 seconds and at least one animal per group")
    root = Path(root).expanduser().resolve()
    if (root / "feeding.yaml").exists() and not force:
        raise FileExistsError(f"{root} already contains a project (use --force to recreate the demo)")
    rng = np.random.default_rng(seed)
    n = int(seconds * FPS)
    groups = {f"m{i + 1:02d}": "Control" if i < animals_per_group else "Treated" for i in range(2 * animals_per_group)}
    names = [f"{s}_{c}_{x}_{a}" for a in groups for s, c, x in SESSIONS]
    (root / "videos").mkdir(parents=True, exist_ok=True)
    pred = root / "data" / "predictions"
    pred.mkdir(parents=True, exist_ok=True)
    cfg_path, _ = init_project(root, name="Demo (synthetic data)", video_names=[f"{v}.mp4" for v in names],
                               chambers=CHAMBERS, conditions=["Pre", "Post"], force=True)
    for i, (animal, group) in enumerate(groups.items()):
        for s, cond, ctx in SESSIONS:
            video = f"{s}_{cond}_{ctx}_{animal}"
            vr = np.random.default_rng([seed, i, int(s)])
            true, pts, scores = simulate(ctx, _behaviour(group, cond, vr), n, vr)
            render(root / "videos" / f"{video}.mp4", ctx, true, vr)
            with h5py.File(pred / f"{video}.analysis.h5", "w") as f:
                f["tracks"] = pts.transpose(2, 1, 0)[None]          # (1, 2, n_nodes, T)
                f["point_scores"] = scores.T[None]                   # (1, n_nodes, T)
                f["node_names"] = np.array([x.encode() for x in NODES])
                f["labels_path"] = "synthetic"
            write_json(pred / f"{video}.provenance.json", {
                "video": video, "source": "demo", "note": "synthetic predictions from feeding.demo", "seed": seed,
                "created_at": now_iso()})
            log(f"demo: {video}")
            if progress:
                progress(i * len(SESSIONS) + SESSIONS.index((s, cond, ctx)) + 1, len(names))
    pd.DataFrame({"animal": list(groups), "group": list(groups.values())}).to_csv(root / "metadata" / "subjects.csv", index=False)
    cfg = update_project(load_project(cfg_path), {
        "skeleton.nodes": NODES, "skeleton.edges": EDGES, "quality.drop_nodes": ["TailTip"], "bouts.node": "Snout",
        "features.speed_nodes": [x for x in NODES if x != "TailTip"],
    })
    if bowls:
        for v in names:
            bx, by, br = ARENAS[v.split("_")[2]]["bowl"]
            save_bowl(cfg.paths.bowls, BowlAnnotation(video=v, center=(bx, by), top=(bx, by - br), bottom=(bx, by + br),
                                                      left=(bx - br, by), right=(bx + br, by), note="demo"))
    (root / "README.md").write_text(
        "# Demo project (synthetic data)\n\nCreated by `feeding demo`: rendered videos of a simulated mouse with "
        "SLEAP-format predictions in `data/predictions/`, so no SLEAP model is needed.\n"
        f"{len(groups)} animals ({animals_per_group} Control, {animals_per_group} Treated), "
        "sessions Pre/Post in arenas A (square) and B (round). Treated animals in Post sessions visit the bowl more "
        "often, feed longer and move more slowly.\n", encoding="utf-8")
    (root / "demo.json").write_text(json.dumps({"seed": seed, "animals_per_group": animals_per_group, "seconds": seconds}), encoding="utf-8")
    return cfg_path
