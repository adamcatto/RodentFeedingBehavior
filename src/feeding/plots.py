"""Static figures for an analysis run: per-session heatmaps and tornado plots, comparison
(view) figures, projections and per-animal box plots."""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, Normalize, TwoSlopeNorm  # noqa: E402
from matplotlib.patches import Ellipse as EllipsePatch  # noqa: E402
from matplotlib.patches import Polygon as PolygonPatch  # noqa: E402

from .config import View  # noqa: E402
from .stats import group_data, select, stars  # noqa: E402

# Validated categorical slots, in fixed order (never cycled): blue, orange, aqua,
# yellow, magenta, green, violet, red. Scatter overlays use at most the first three.
CAT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
PAIR = CAT[:2]
SEQ = LinearSegmentedColormap.from_list(
    "seq_blue", ["#fcfcfb", "#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"]
)
# Diverging blue <-> red with a neutral grey midpoint.
DIV = LinearSegmentedColormap.from_list(
    "div_blue_red", ["#184f95", "#3987e5", "#9ec5f4", "#f0efec", "#f3b1ab", "#e34948", "#a32a2b"]
)
INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e4e3df"

plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 200, "savefig.bbox": "tight",
    "font.size": 9, "axes.titlesize": 10, "axes.labelcolor": INK_2, "axes.edgecolor": GRID,
    "xtick.color": INK_2, "ytick.color": INK_2, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False,
})


def color_of(i: int) -> str:
    return CAT[i] if i < len(CAT) else MUTED


def _wrap(s: str, width: int = 14) -> str:
    return "\n".join(textwrap.wrap(s, width)) or s


def _save(fig, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)


def _box(ax, vals: pd.Series, x: float, color: str, width: float, unit: str, seed: int = 0) -> None:
    v = vals.dropna()
    if not len(v):
        return
    ax.boxplot([v], positions=[x], widths=width, patch_artist=True, showfliers=False,
               boxprops=dict(facecolor=color, alpha=0.3, edgecolor=color, linewidth=1),
               medianprops=dict(color=color, linewidth=2), whiskerprops=dict(color=color), capprops=dict(color=color))
    jitter = np.random.default_rng(seed).uniform(-width * 0.22, width * 0.22, len(v))
    ax.scatter(x + jitter, v, s=6 if unit == "bout" else 18, color=color, alpha=0.45 if unit == "bout" else 0.95,
               linewidths=0, zorder=3)


def _pair_lines(ax, A: pd.Series, B: pd.Series, xa: float, xb: float) -> None:
    common = A.dropna().index.intersection(B.dropna().index)
    for k in common:
        ax.plot([xa, xb], [A[k], B[k]], color=MUTED, linewidth=0.6, alpha=0.6, zorder=2)


def view_boxplot(df: pd.DataFrame, st: pd.DataFrame, view: View, feature: str, unit: str, out: Path) -> None:
    """One feature across a view's groups.

    Explicit pairs (e.g. the standard view): two boxes per pair side by side with
    the adjusted p under each pair. All-pairs views: one
    box per group with significance brackets. Paired animals are joined by lines.
    """
    labels = [g.label for g in view.groups]
    color = {lbl: color_of(i) for i, lbl in enumerate(labels)}
    data = {g.label: group_data(df, g, unit) for g in view.groups}
    col = lambda lbl: data[lbl][feature] if feature in data[lbl] else pd.Series(dtype=float)  # noqa: E731
    res = st[st["feature"] == feature].set_index("pair") if len(st) else pd.DataFrame()
    pairs = view.pair_list()

    if view.pairs:  # pair-by-pair layout
        fig, ax = plt.subplots(figsize=(max(6.0, 1.45 * len(pairs) + 1.2), 4.8))
        ticks, ticklabels = [], []
        allv = pd.concat([col(lbl) for pair in pairs for lbl in pair]).dropna()
        lo, hi = (allv.min(), allv.max()) if len(allv) else (0.0, 1.0)
        span = (hi - lo) or 1.0
        for k, (a, b) in enumerate(pairs):
            x0 = k * 1.4
            A, B = col(a), col(b)
            _box(ax, A, x0, color[a], 0.42, unit, seed=k)
            _box(ax, B, x0 + 0.5, color[b], 0.42, unit, seed=k + 100)
            r = res.loc[f"{a} vs {b}"] if f"{a} vs {b}" in res.index else None
            if r is not None and bool(r["paired"]):
                _pair_lines(ax, A, B, x0, x0 + 0.5)
            ticks += [x0, x0 + 0.5]
            ticklabels += [a, b]
            y = hi + span * 0.08
            ax.plot([x0, x0, x0 + 0.5, x0 + 0.5], [y - span * 0.02, y, y, y - span * 0.02], color=INK_2, linewidth=0.8)
            txt = "n/a" if r is None or not np.isfinite(r["p_adj"]) else f"{r['stars']}\np={r['p_adj']:.2g}"
            ax.text(x0 + 0.25, y, txt, ha="center", va="bottom", fontsize=7, color=INK, linespacing=1.1)
        ax.set_ylim(lo - span * 0.05, hi + span * 0.3)
        ax.set_xticks(ticks, ticklabels, fontsize=7, rotation=40, ha="right", rotation_mode="anchor")
        ax.set_xlim(-0.5, (len(pairs) - 1) * 1.4 + 1.0)
    else:  # one box per group + brackets
        fig, ax = plt.subplots(figsize=(max(3.6, 1.05 * len(labels) + 1.4), 4.4))
        for i, lbl in enumerate(labels):
            _box(ax, col(lbl), i, color[lbl], 0.55, unit, seed=i)
        for a, b in pairs:
            r = res.loc[f"{a} vs {b}"] if f"{a} vs {b}" in res.index else None
            if r is not None and bool(r["paired"]) and abs(labels.index(a) - labels.index(b)) == 1:
                _pair_lines(ax, col(a), col(b), labels.index(a), labels.index(b))
        allv = pd.concat([col(lbl) for lbl in labels]).dropna()
        if len(allv):
            lo, hi = allv.min(), allv.max()
            span = (hi - lo) or 1.0
            levels: list[list[tuple[int, int]]] = []
            shown = [(a, b) for a, b in pairs if col(a).notna().any() and col(b).notna().any()]
            for a, b in sorted(shown, key=lambda p: abs(labels.index(p[0]) - labels.index(p[1]))):
                i, j = sorted((labels.index(a), labels.index(b)))
                lvl = next((n for n, used in enumerate(levels) if all(j < s or i > e for s, e in used)), len(levels))
                if lvl == len(levels):
                    levels.append([])
                levels[lvl].append((i, j))
                y = hi + span * (0.08 + 0.11 * lvl)
                r = res.loc[f"{a} vs {b}"] if f"{a} vs {b}" in res.index else None
                txt = (r["stars"] or "n/a") if r is not None else "n/a"
                ax.plot([i, i, j, j], [y - span * 0.02, y, y, y - span * 0.02], color=INK_2, linewidth=0.8)
                ax.text((i + j) / 2, y, txt, ha="center", va="bottom", fontsize=8, color=INK)
            ax.set_ylim(lo - span * 0.05, hi + span * (0.15 + 0.11 * len(levels)))
        ax.set_xticks(range(len(labels)), [_wrap(lbl) for lbl in labels], fontsize=7.5)
        ax.set_xlim(-0.6, len(labels) - 0.4)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel(feature)
    ax.set_title(f"{feature}", loc="left", color=INK, pad=18)
    ax.text(0, 1.01, f"{view.name} · unit: {unit} · stars: adjusted p", transform=ax.transAxes, ha="left",
            va="bottom", fontsize=7, color=MUTED)
    _save(fig, out)


def overview_heatmap(st: pd.DataFrame, title: str, out: Path, limit: float = 1.5) -> None:
    """Features x pairs, coloured by effect size (b - a), stars where adjusted p < 0.05."""
    if not len(st):
        return
    pairs = list(dict.fromkeys(st["pair"]))
    feats = list(dict.fromkeys(st["feature"]))
    es = st.pivot(index="feature", columns="pair", values="effect_size").reindex(index=feats, columns=pairs)
    pa = st.pivot(index="feature", columns="pair", values="p_adj").reindex(index=feats, columns=pairs)
    fig, ax = plt.subplots(figsize=(2.6 + 0.95 * len(pairs), 1.2 + 0.24 * len(feats)))
    m = np.ma.masked_invalid(es.to_numpy(dtype=float))
    cmap = DIV.copy()
    cmap.set_bad("#f4f3f0")
    im = ax.imshow(m, cmap=cmap, norm=TwoSlopeNorm(0, -limit, limit), aspect="auto")
    for i in range(len(feats)):
        for j in range(len(pairs)):
            p = pa.iat[i, j]
            if np.isfinite(p) and p < 0.05:
                v = es.iat[i, j]
                ax.text(j, i, stars(p), ha="center", va="center", fontsize=7,
                        color="#ffffff" if np.isfinite(v) and abs(v) > 0.9 * limit else INK)
    ax.set_xticks(range(len(pairs)), [_wrap(p, 16) for p in pairs], fontsize=7, rotation=0)
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(feats)), feats, fontsize=7)
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    cb = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label("effect size, second minus first (Hedges g / d_z)")
    ax.set_title(title, loc="left", color=INK, pad=36 if len(pairs) > 1 else 24)
    _save(fig, out)


def _kde(x: np.ndarray, y: np.ndarray, extent: float, n: int = 120) -> np.ndarray:
    from scipy.stats import gaussian_kde

    ok = np.isfinite(x) & np.isfinite(y)
    xs = np.linspace(-extent, extent, n)
    X, Y = np.meshgrid(xs, xs)
    if ok.sum() < 10:
        return np.zeros_like(X)
    rng = np.random.default_rng(0)
    idx = np.flatnonzero(ok)
    if len(idx) > 20_000:  # subsample for speed; fixed seed keeps it reproducible
        idx = rng.choice(idx, 20_000, replace=False)
    return gaussian_kde(np.vstack([x[idx], y[idx]]))(np.vstack([X.ravel(), Y.ravel()])).reshape(X.shape)


def rim_patch(rim: dict, color: str = PAIR[1], alpha: float = 1.0, linewidth: float = 1.5):
    style = dict(fill=False, edgecolor=color, linewidth=linewidth, alpha=alpha)
    if rim.get("type") == "polygon":
        return PolygonPatch(rim["points"], closed=True, **style)
    return EllipsePatch((rim["cx"], rim["cy"]), 2 * rim["semi_major"], 2 * rim["semi_minor"],
                        angle=np.degrees(rim["angle_rad"]), **style)


def session_heatmaps(sessions: list[tuple[pd.DataFrame, dict, str]], node: str, out: Path, extent: float = 120.0) -> None:
    """Bowl-centred density of ``node`` for each session (tracks, rim, title), on one colour scale.

    Tracks are already relative to the clicked bowl centre; rims are in that frame.
    """
    dens = []
    for t, _, _ in sessions:
        v = t[t["valid"]]
        dens.append(_kde(v[f"{node}_x"].to_numpy(), v[f"{node}_y"].to_numpy(), extent))
    norm = Normalize(0, max(d.max() for d in dens) or 1)
    n = len(sessions)
    fig, axes = plt.subplots(1, n, figsize=(4.6 * n + 0.8, 4.6), constrained_layout=True, squeeze=False)
    for ax, d, (_, rim, title) in zip(axes[0], dens, sessions):
        im = ax.imshow(d, extent=(-extent, extent, extent, -extent), cmap=SEQ, norm=norm)
        ax.add_patch(rim_patch(rim))
        ax.set_title(title, loc="left", color=INK, fontsize=9)
        ax.set_xlabel("x from bowl centre (px)")
        ax.set_ylabel("y from bowl centre (px)")
        ax.grid(False)
    fig.colorbar(im, ax=axes[0].tolist(), shrink=0.8, label=f"{node} position density")
    _save(fig, out)


def session_tornados(sessions: list[tuple[pd.DataFrame, dict, str]], node: str, out: Path,
                     color_by: str = "distance", smooth: int = 10) -> None:
    """3-D trajectory (x, y, time) of ``node`` (bowl-centred tracks), one panel per session."""
    from matplotlib.collections import LineCollection  # noqa: F401  (registers 3-D collections)
    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    n = len(sessions)
    fig = plt.figure(figsize=(5.6 * n, 6))
    for k, (t, _, title) in enumerate(sessions):
        ax = fig.add_subplot(1, n, k + 1, projection="3d")
        v = t[t["valid"]]
        x = v[f"{node}_x"].rolling(smooth).mean().to_numpy()
        y = v[f"{node}_y"].rolling(smooth).mean().to_numpy()
        z = v.index.to_numpy(dtype=float)
        val = np.hypot(x, y) if color_by == "distance" else np.r_[np.nan, np.hypot(np.diff(x), np.diff(y))]
        lo, hi = np.nanquantile(val, [0.01, 0.99]) if np.isfinite(val).any() else (0, 1)
        pts = np.column_stack([x, y, z])
        segs = np.stack([pts[:-1], pts[1:]], axis=1) if len(pts) > 1 else np.zeros((0, 2, 3))
        # Do not draw across gaps between valid segments.
        keep = (np.diff(z) == 1) & np.isfinite(segs).all(axis=(1, 2)) if len(pts) > 1 else np.zeros(0, bool)
        lc = Line3DCollection(segs[keep], cmap=SEQ, norm=Normalize(lo, hi), linewidths=0.6)
        lc.set_array(val[:-1][keep] if len(val) > 1 else np.zeros(0))
        ax.add_collection3d(lc)
        z0, z1 = (z.min(), z.max()) if len(z) else (0, 1)
        ax.plot([0, 0], [0, 0], [z0, z1], color=PAIR[1], linewidth=1.5)
        lim = np.nanmax(np.abs(np.r_[x, y])) if np.isfinite(np.r_[x, y]).any() else 1
        ax.set(xlim=(-lim, lim), ylim=(-lim, lim), zlim=(z0, z1 if z1 > z0 else z0 + 1),
               xlabel=f"{node} x (px)", ylabel=f"{node} y (px)", zlabel="frame")
        ax.set_title(title, loc="left", color=INK, fontsize=9)
        fig.colorbar(lc, ax=ax, orientation="horizontal", fraction=0.04, pad=0.08,
                     label="distance from bowl centre (px)" if color_by == "distance" else "speed (px/frame)")
    _save(fig, out)


def animal_boxplot(feats: pd.DataFrame, feature: str, conditions: list[str], out: Path, by: str = "condition") -> None:
    """One feature's bout values for every animal, one box per value of ``by`` (in the order
    ``conditions``; colour = that value), e.g. condition, or condition and context.

    Animals are ordered by group, then id; the group is written under the animal.
    """
    from .naming import natural_key

    df = feats[["animal", "group", by, feature]].rename(columns={by: "_by"}).dropna(subset=[feature]).copy()
    if not len(df):
        return
    df["group"] = df["group"].fillna("no group")
    animals = (df[["animal", "group"]].drop_duplicates("animal")
               .assign(k=lambda d: [(natural_key(str(g)), natural_key(str(a))) for g, a in zip(d["group"], d["animal"])])
               .sort_values("k"))
    conds = [c for c in conditions if c in set(df["_by"])]
    width = 0.8 / max(1, len(conds))
    fig, ax = plt.subplots(figsize=(max(5.0, 0.35 + len(animals) * (0.28 + 0.22 * len(conds))), 4.2))
    for i, a in enumerate(animals["animal"]):
        for j, c in enumerate(conds):
            v = df.loc[(df["animal"] == a) & (df["_by"] == c), feature]
            if len(v):
                _box(ax, v, i - 0.4 + width * (j + 0.5), color_of(j), width * 0.85, "bout", seed=i * 10 + j)
    ax.set_xticks(range(len(animals)), [f"#{a}\n{g}" for a, g in zip(animals["animal"], animals["group"])], fontsize=7.5)
    ax.set_xlim(-0.6, len(animals) - 0.4)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel(feature)
    ax.set_title(f"{feature} per animal (each point is a bout)", loc="left", color=INK)
    from matplotlib.patches import Patch

    ax.legend(handles=[Patch(color=color_of(j), alpha=0.6, label=c) for j, c in enumerate(conds)],
              title=by.replace("_", " "), loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8)
    _save(fig, out)


# --------------------------------------------------------------------------- occupancy


def group_occupancy(occ: dict[str, np.ndarray], meta: pd.DataFrame, where: dict) -> tuple[np.ndarray | None, int]:
    """Mean over animals of each animal's normalised occupancy (videos pooled per animal)."""
    from scipy.ndimage import gaussian_filter

    sub = meta[select(meta, where)]
    maps = []
    for _, vids in sub.groupby("animal")["video"]:
        hs = [occ[v] for v in vids if v in occ]
        h = np.sum(hs, axis=0) if hs else None
        if h is not None and h.sum() > 0:
            maps.append(h / h.sum())
    if not maps:
        return None, 0
    return gaussian_filter(np.mean(maps, axis=0), 1.2), len(maps)


def view_occupancy(occ: dict[str, np.ndarray], extent: float, rims: dict[str, dict], meta: pd.DataFrame,
                   view: View, node: str, out: Path) -> None:
    """Bowl-centred ``node`` occupancy per group (shared scale) and the difference for each pair."""
    labels = [g.label for g in view.groups]
    maps = {g.label: group_occupancy(occ, meta, g.where) for g in view.groups}
    pairs = [(a, b) for a, b in view.pair_list() if maps[a][0] is not None and maps[b][0] is not None]
    n_g, n_p = len(labels), len(pairs)
    cols = min(4, max(n_g, n_p, 1))
    rows_g, rows_p = -(-n_g // cols), -(-n_p // cols)
    fig, axes = plt.subplots(rows_g + rows_p, cols, figsize=(2.7 * cols + 1.2, 2.6 * (rows_g + rows_p) + 0.4),
                             squeeze=False, constrained_layout=True)
    vmax = max((m.max() for m, _ in maps.values() if m is not None), default=1)
    ext = (-extent, extent, extent, -extent)
    # Zoom to where any group has data (symmetric about the bowl centre).
    nz = [np.argwhere(m > vmax * 0.02) for m, _ in maps.values() if m is not None]
    nz = np.vstack(nz) if nz and any(len(z) for z in nz) else np.zeros((0, 2))
    n_bins = next((m.shape[0] for m, _ in maps.values() if m is not None), 1)
    if len(nz):
        far = np.abs((nz + 0.5) / n_bins * 2 * extent - extent).max()
        lim = min(extent, np.ceil((far + 15) / 10) * 10)
    else:
        lim = extent
    im_g = im_d = None
    dmax = max((np.abs(maps[b][0] - maps[a][0]).max() for a, b in pairs), default=1) or 1
    for k, lbl in enumerate(labels):
        ax = axes[k // cols][k % cols]
        m, n = maps[lbl]
        ax.grid(False)
        if m is None:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", color=MUTED)
            ax.set_xticks([]), ax.set_yticks([])
        else:
            im_g = ax.imshow(m, extent=ext, cmap=SEQ, norm=Normalize(0, vmax))
            g = view.groups[k]
            for v in meta[select(meta, g.where)]["video"]:
                if v in rims:
                    ax.add_patch(rim_patch(rims[v], color=CAT[1], alpha=0.35, linewidth=0.7))
        ax.set_title(f"{lbl}  (n={n} animals)", loc="left", color=INK, fontsize=8.5)
        ax.set(xlim=(-lim, lim), ylim=(lim, -lim))
        ax.tick_params(labelsize=7)
    for k, (a, b) in enumerate(pairs):
        ax = axes[rows_g + k // cols][k % cols]
        ax.grid(False)
        im_d = ax.imshow(maps[b][0] - maps[a][0], extent=ext, cmap=DIV, norm=TwoSlopeNorm(0, -dmax, dmax))
        ax.set_title(_wrap(f"{b} minus {a}", 34), loc="left", color=INK, fontsize=8.5)
        ax.set(xlim=(-lim, lim), ylim=(lim, -lim))
        ax.tick_params(labelsize=7)
    for r in range(rows_g + rows_p):
        used = n_g if r < rows_g else n_p
        start = (r if r < rows_g else r - rows_g) * cols
        for c in range(cols):
            if start + c >= used:
                axes[r][c].axis("off")
    if im_g is not None:
        fig.colorbar(im_g, ax=axes[:rows_g].ravel().tolist(), shrink=0.8, label=f"{node} occupancy (fraction of valid frames)")
    if im_d is not None:
        fig.colorbar(im_d, ax=axes[rows_g:].ravel().tolist(), shrink=0.8, label="difference in occupancy")
    fig.suptitle(f"{view.name}: where the {node} spends time, relative to the bowl centre (px)", x=0.01, ha="left",
                 color=INK, fontsize=10)
    _save(fig, out)


# --------------------------------------------------------------------------- projections


def scatter_by(coords: np.ndarray, labels: pd.Series, method: str, title: str, out: Path, order: list[str] | None = None) -> None:
    """2-D projection coloured by ``labels``: overlaid for up to three levels,
    otherwise small multiples (each level over the others in grey)."""
    labels = labels.astype(str).reset_index(drop=True)
    levels = order or sorted(labels.dropna().unique())
    if len(levels) <= 3:
        fig, ax = plt.subplots(figsize=(5.6, 5.0))
        for i, lvl in enumerate(levels):
            m = (labels == lvl).to_numpy()
            ax.scatter(coords[m, 0], coords[m, 1], s=9, alpha=0.5, color=CAT[i], linewidths=0, label=f"{lvl} (n={m.sum()})")
        ax.legend(loc="best", fontsize=7.5, markerscale=1.6)
        ax.set(xlabel=f"{method} 1", ylabel=f"{method} 2")
        ax.set_title(title, loc="left", color=INK)
    else:
        cols = min(4, len(levels))
        rows = -(-len(levels) // cols)
        fig, axes = plt.subplots(rows, cols, figsize=(3.0 * cols, 2.9 * rows), sharex=True, sharey=True,
                                 squeeze=False, constrained_layout=True)
        for k, lvl in enumerate(levels):
            ax = axes[k // cols][k % cols]
            m = (labels == lvl).to_numpy()
            ax.scatter(coords[~m, 0], coords[~m, 1], s=4, color=GRID, linewidths=0)
            ax.scatter(coords[m, 0], coords[m, 1], s=6, alpha=0.6, color=color_of(k), linewidths=0)
            ax.set_title(f"{lvl} (n={m.sum()})", loc="left", fontsize=8, color=INK)
        for k in range(len(levels), rows * cols):
            axes[k // cols][k % cols].axis("off")
        fig.suptitle(title, x=0.01, ha="left", color=INK, fontsize=10)
    _save(fig, out)


def projection_scatter(coords: np.ndarray, meta: pd.DataFrame, method: str, out_dir: Path) -> None:
    """Projection figures: everything, by group / condition / context /
    context x condition, and condition within each context."""
    meta = meta.reset_index(drop=True)
    m = method.lower()
    fig, ax = plt.subplots(figsize=(5.6, 5.0))
    ax.scatter(coords[:, 0], coords[:, 1], s=6, alpha=0.4, color=CAT[0], linewidths=0)
    ax.set(xlabel=f"{method} 1", ylabel=f"{method} 2")
    ax.set_title(f"Bout features, {method} (n={len(coords)} bouts)", loc="left", color=INK)
    _save(fig, out_dir / f"{m}_all.png")
    for col in ("group", "condition", "context"):
        if col in meta and meta[col].nunique() >= 2:
            scatter_by(coords, meta[col], method, f"Bout features, {method}, by {col}", out_dir / f"{m}_by_{col}.png")
    if {"context", "condition"} <= set(meta.columns):
        scatter_by(coords, meta["condition"].astype(str) + " " + meta["context"].astype(str), method,
                   f"Bout features, {method}, by condition x context", out_dir / f"{m}_by_condition_context.png")
        for ctx, idx in meta.groupby("context").indices.items():
            if meta.loc[idx, "condition"].nunique() >= 2:
                scatter_by(coords[idx], meta.loc[idx, "condition"], method,
                           f"Context {ctx}: {method} by condition", out_dir / f"{m}_context_{ctx}_by_condition.png")


def local_clustering_box(clus: pd.DataFrame, test: dict, label: str, k: int, out: Path) -> None:
    """Same-condition neighbours per bout, compared between contexts."""
    if not len(clus):
        return
    ctxs = sorted(clus["context"].unique())
    fig, ax = plt.subplots(figsize=(4.6, 4.4))
    for i, c in enumerate(ctxs):
        _box(ax, clus.loc[clus["context"] == c, "metric"], i, CAT[0], 0.5, "bout", seed=i)
    ax.set_xticks(range(len(ctxs)), [f"context {c}" for c in ctxs])
    if test and len(ctxs) == 2:
        hi = clus["metric"].max()
        y = hi * 1.06 if hi > 0 else 1
        ax.plot([0, 0, 1, 1], [y * 0.98, y, y, y * 0.98], color=INK_2, linewidth=0.8)
        ax.text(0.5, y, f"{stars(test['pvalue'])}  p={test['pvalue']:.3g}", ha="center", va="bottom", fontsize=8)
    ax.set_ylabel(f"of {k} nearest neighbours: '{label}' bouts,\nnormalised by its share")
    ax.set_title("Local same-condition clustering (PCA space)", loc="left", color=INK)
    ax.grid(axis="x", visible=False)
    _save(fig, out)


def rf_importance(rf: pd.DataFrame, out: Path, top: int = 15) -> None:
    """Random-forest feature importances per context, with held-out AUC."""
    if not len(rf):
        return
    ctxs = list(dict.fromkeys(rf["context"]))
    fig, axes = plt.subplots(1, len(ctxs), figsize=(4.8 * len(ctxs), 0.28 * top + 1.4), squeeze=False, constrained_layout=True)
    for ax, ctx in zip(axes[0], ctxs):
        sub = rf[rf["context"] == ctx].nlargest(top, "importance").iloc[::-1]
        ax.barh(sub["feature"], sub["importance"], color=CAT[0], height=0.6)
        ax.set_title(f"Context {ctx}: AUC {sub['auc'].iloc[0]:.2f}", loc="left", color=INK)
        ax.set_xlabel("importance")
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(axis="y", visible=False)
    fig.suptitle("Random forest, first vs last condition", x=0.01, ha="left", color=INK, fontsize=10)
    _save(fig, out)
