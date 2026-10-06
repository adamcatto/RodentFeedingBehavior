"""Run comparison views against an analysis run.

A view only needs a run's tables (``bout_features.csv``, ``sessions.csv``) and
its occupancy maps, so views can be added or edited and re-run on an existing
run in seconds, without re-processing any video. Each view writes::

    views/<slug>/
        view.json            definition, statistics settings, group sizes, timestamp
        groups.csv           what each group matched (animals, videos, bouts)
        stats_bouts.csv      every pair x bout feature (unit = analysis.unit)
        stats_sessions.csv   every pair x session measure (per animal), incl. locomotion;
                             videos of a session recorded in parts are combined first
        omnibus_*.csv        one test across all groups (views with 3+ groups)
        stats.xlsx           all of the above, one sheet each (+ one sheet per pair)
        figures/             overview heatmaps, a boxplot per feature, occupancy maps, PCA
    views/index.json         one summary line per view
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from . import exploratory, plots
from .config import AnalysisConfig, Config, View
from .naming import natural_key
from .provenance import now_iso, write_json
from .stats import analysis_conditions, compare_view, group_sizes, omnibus, select, standard_view, write_excel

Log = Callable[[str], None]
META_DTYPES = {"animal": str, "session": str, "part": str, "condition": str, "context": str, "group": str,
               "chamber": str, "video": str}


def load_run_tables(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    def read(name):
        p = run_dir / name
        return pd.read_csv(p, dtype=META_DTYPES) if p.exists() else pd.DataFrame()

    return read("bout_features.csv"), read("sessions.csv")


def load_occupancy(run_dir: Path) -> tuple[dict[str, np.ndarray], float, dict[str, dict]]:
    p = run_dir / "occupancy.npz"
    if not p.exists():
        return {}, 0.0, {}
    z = np.load(p)
    occ = {k: z[k] for k in z.files if k != "__extent__"}
    rims_p = run_dir / "occupancy_rims.json"
    return occ, float(z["__extent__"]), json.loads(rims_p.read_text()) if rims_p.exists() else {}


# Session columns that describe the recording rather than behaviour: kept in sessions.csv, not tested.
SESSION_BOOKKEEPING = ["n_frames", "valid_frames", "in_bowl_frames"]
_SUMMED = ("n_frames", "valid_frames", "in_bowl_frames", "in_bowl_time_s", "n_bouts")


def combine_parts(sessions: pd.DataFrame) -> pd.DataFrame:
    """One row per recording session: videos of a session recorded in parts
    (``part`` 1, 2, ...) are merged. Counts and durations are summed; rates,
    fractions and means are recomputed or weighted by valid frames; the latency
    to first contact counts from the start of the first part."""
    if "part" not in sessions or sessions["part"].isna().all() or not len(sessions):
        return sessions
    keys = [c for c in ("animal", "session", "condition", "context", "chamber", "group") if c in sessions]
    fps = (sessions["in_bowl_frames"] / sessions["in_bowl_time_s"]).replace([np.inf, -np.inf], np.nan).dropna()
    fps = float(fps.median()) if len(fps) else 30.0
    rows = []
    for _, g in sessions.groupby(keys, dropna=False, sort=False):
        if len(g) == 1:
            rows.append(g.iloc[0].to_dict())
            continue
        g = g.sort_values("part", key=lambda s: pd.to_numeric(s, errors="coerce"))
        r = {k: g[k].iloc[0] for k in keys}
        r["video"] = "+".join(g["video"])
        r["part"] = None
        w = g["valid_frames"].clip(lower=0)
        for c in g.columns:
            if c in r or c in keys or not pd.api.types.is_numeric_dtype(g[c]):
                continue
            if c in _SUMMED or c.endswith("_distance_px"):
                r[c] = g[c].sum(min_count=1)
            elif c.endswith("moving_speed_mean"):
                ww = w * g[c.replace("moving_speed_mean", "moving_fraction")].fillna(0)
                r[c] = np.average(g[c], weights=ww) if g[c].notna().all() and ww.sum() > 0 else g[c].mean()
            else:
                ok = g[c].notna() & (w > 0)
                r[c] = np.average(g.loc[ok, c], weights=w[ok]) if ok.any() else np.nan
        r["valid_fraction"] = r["valid_frames"] / r["n_frames"] if r["n_frames"] else np.nan
        r["in_bowl_fraction_of_valid"] = r["in_bowl_frames"] / r["valid_frames"] if r["valid_frames"] else np.nan
        r["bouts_per_valid_min"] = r["n_bouts"] / (r["valid_frames"] / fps / 60) if r["valid_frames"] else np.nan
        nb = g["n_bouts"].fillna(0)
        r["mean_bout_frames"] = np.average(g["mean_bout_frames"].fillna(0), weights=nb) if nb.sum() else np.nan
        lat, offset = np.nan, 0.0
        for _, part in g.iterrows():
            if np.isfinite(part["latency_to_first_contact_s"]):
                lat = offset + part["latency_to_first_contact_s"]
                break
            offset += part["n_frames"] / fps
        r["latency_to_first_contact_s"] = lat
        rows.append(r)
    return pd.DataFrame(rows, columns=sessions.columns)


def views_for(cfg: Config, sessions: pd.DataFrame) -> list[View]:
    """The project's views, preceded by the standard view when enabled."""
    out = []
    if cfg.analysis.standard_view and len(sessions):
        groups = sorted(sessions["group"].dropna().unique(), key=natural_key)
        conds = analysis_conditions(cfg.analysis.conditions, sessions)
        ctxs = sorted(c for c in sessions["context"].dropna().unique() if c != "NA")
        sv = standard_view(conds, groups, ctxs)
        if sv and not any(v.slug == sv.slug for v in cfg.views):
            out.append(sv)
    return out + list(cfg.views)


def run_view(run_dir: Path, view: View, analysis: AnalysisConfig, node: str, make_plots: bool = True,
             log: Log = print, tables=None, occupancy=None) -> dict:
    feats, sessions = tables or load_run_tables(run_dir)
    out = run_dir / "views" / view.slug
    if out.exists():
        shutil.rmtree(out)
    (out / "figures").mkdir(parents=True)
    log(f"view '{view.name}': {len(view.groups)} groups, {len(view.pair_list())} pairs")

    per_session = combine_parts(sessions).drop(columns=SESSION_BOOKKEEPING, errors="ignore")
    sizes = group_sizes(per_session, view) if len(sessions) else pd.DataFrame()
    if len(feats) and len(sizes):
        sizes["bouts"] = [int(select(feats, g.where).sum()) for g in view.groups]
    sizes.to_csv(out / "groups.csv", index=False)

    st_b = compare_view(feats, view, analysis) if len(feats) else pd.DataFrame()
    st_s = compare_view(per_session, view, analysis, unit="animal") if len(sessions) else pd.DataFrame()
    om_b = omnibus(feats, view, analysis) if len(feats) and not view.pairs else pd.DataFrame()
    om_s = omnibus(per_session, view, analysis, unit="animal") if len(sessions) and not view.pairs else pd.DataFrame()
    sheets = {"groups": sizes}
    for name, df in (("stats_bouts", st_b), ("stats_sessions", st_s), ("omnibus_bouts", om_b), ("omnibus_sessions", om_s)):
        if len(df):
            df.to_csv(out / f"{name}.csv", index=False)
            sheets[name.replace("_", " ")] = df
    for pair, sub in st_b.groupby("pair", sort=False) if len(st_b) else []:
        sheets[pair] = sub.sort_values("pvalue")
    write_excel(out / "stats.xlsx", sheets)

    if make_plots:
        fig = out / "figures"
        if len(st_b):
            plots.overview_heatmap(st_b, f"{view.name}: bout features", fig / "overview_bouts.png")
        if len(st_s):
            plots.overview_heatmap(st_s, f"{view.name}: session measures (per animal)", fig / "overview_sessions.png")
        for st, df, unit, sub in ((st_b, feats, analysis.unit, "boxplots_bouts"), (st_s, per_session, "animal", "boxplots_sessions")):
            for f in dict.fromkeys(st["feature"]) if len(st) else []:
                plots.view_boxplot(df, st, view, f, unit, fig / sub / f"{f}.png")
        occ, extent, rims = occupancy or load_occupancy(run_dir)
        if occ and len(sessions):
            plots.view_occupancy(occ, extent, rims, sessions, view, node, fig / "occupancy.png")
        if len(feats):
            rows = [(g.label, feats[select(feats, g.where)]) for g in view.groups]
            both = pd.concat([d.assign(_view_group=lbl) for lbl, d in rows], ignore_index=True)
            if len(both) >= 3:
                try:
                    coords, kept, _ = exploratory.project(both, "PCA", analysis.seed, exploratory.pca_features(node))
                    plots.scatter_by(coords, kept["_view_group"], "PCA", f"{view.name}: bout features, PCA",
                                     fig / "pca.png", order=[g.label for g in view.groups])
                except ValueError:
                    pass

    sig = lambda st: int((st["p_adj"] < 0.05).sum()) if len(st) else 0  # noqa: E731
    summary = {
        "slug": view.slug, "name": view.name, "description": view.description,
        "n_groups": len(view.groups), "n_pairs": len(view.pair_list()),
        "n_sig_bouts": sig(st_b), "n_sig_sessions": sig(st_s), "created_at": now_iso(),
    }
    write_json(out / "view.json", {
        **summary, "definition": view.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"), "groups": sizes.to_dict(orient="records"),
    })
    return summary


def run_views(run_dir: Path, cfg: Config, views: list[View] | None = None, make_plots: bool = True,
              log: Log = print) -> list[dict]:
    """Run ``views`` (default: the standard view + the project's views) on a run."""
    tables = load_run_tables(run_dir)
    views = views if views is not None else views_for(cfg, tables[1])
    occupancy = load_occupancy(run_dir)
    index_p = run_dir / "views" / "index.json"
    index = {v["slug"]: v for v in (json.loads(index_p.read_text()) if index_p.exists() else [])}
    done = []
    for v in views:
        try:
            s = run_view(run_dir, v, cfg.analysis, cfg.bouts.node, make_plots, log, tables, occupancy)
        except Exception as exc:  # one bad view must not lose the others
            log(f"  ! view '{v.name}': {exc}")
            s = {"slug": v.slug, "name": v.name, "error": str(exc), "created_at": now_iso()}
        index[v.slug] = s
        done.append(s)
    index_p.parent.mkdir(parents=True, exist_ok=True)
    write_json(index_p, list(index.values()))
    return done
