"""End-to-end analysis run: tracks -> bouts -> features -> stats -> figures.

Each run writes a new directory ``paths.results/{timestamp}_{config-hash}/``::

    manifest.json        config snapshot + hash, git commit, package versions,
                         per-video input hashes and prediction provenance, skips
    feeding.yaml         verbatim copy of the project config used
    bouts.csv            one row per bout (frame indices)
    bout_features.csv    bout features + video metadata
    sessions.csv         per-video summary + locomotion measures + metadata
    occupancy.npz        per-video bowl-centred histogram of the bout node (for group maps)
    views/               one folder per comparison view: statistics, Excel, figures
                         (see feeding/views.py), starting with the standard comparisons
    exploratory/         PCA (+UMAP), local clustering, random forest, with figures
    figures/             per animal x context: heatmaps and tornado plots, one panel per session
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import exploratory, plots
from .bouts import detect_bouts
from .bowl import bowl_path, load_bowl, shift_rim
from .config import Config
from .features import bout_features, locomotion, session_summary
from .naming import list_videos, load_subjects, video_metadata
from .provenance import git_state, now_iso, package_versions, sha256, write_json
from .sleap_runner import has_predictions, prediction_paths
from .stats import analysis_conditions
from .tracks import load_tracks, missing_inputs
from .views import run_views

OCC_EXTENT = 160.0  # px either side of the bowl centre
OCC_BINS = 80

Log = Callable[[str], None]


def video_status(cfg: Config) -> pd.DataFrame:
    """Every video in ``paths.videos`` with metadata and pipeline readiness."""
    paths = list_videos(cfg.paths.videos, cfg.naming.pattern)
    meta = video_metadata([p.stem for p in paths], load_subjects(cfg.paths.subjects),
                          cfg.naming.pattern, cfg.naming.chambers)
    meta["path"] = [str(p) for p in paths]
    meta["has_predictions"] = [has_predictions(cfg, p.stem) for p in paths]
    meta["has_bowl"] = [bowl_path(cfg.paths.bowls, p.stem).exists() for p in paths]
    return meta


def process_video(cfg: Config, video: str, video_path: Path) -> dict:
    tracks, info = load_tracks(cfg, video, video_path)
    bouts = detect_bouts(tracks, cfg.bouts)
    cx, cy = info["center"]
    feats = bout_features(tracks, bouts, cx, cy, info["fps"], cfg)
    f = cfg.features
    loco = locomotion(tracks, f.body_node, cx, cy, info["fps"], f.moving_speed) if f.body_node else {}
    return {"tracks": tracks, "info": info, "bouts": bouts, "features": feats,
            "summary": session_summary(tracks, bouts, info["fps"], loco)}


def occupancy(tracks: pd.DataFrame, info: dict, node: str) -> np.ndarray:
    """2-D histogram (rows = y) of ``node`` over valid frames, relative to the bowl centre."""
    cx, cy = info["center"]
    v = tracks[tracks["valid"]]
    x, y = v[f"{node}_x"].to_numpy() - cx, v[f"{node}_y"].to_numpy() - cy
    ok = np.isfinite(x) & np.isfinite(y)
    edges = np.linspace(-OCC_EXTENT, OCC_EXTENT, OCC_BINS + 1)
    h, _, _ = np.histogram2d(y[ok], x[ok], bins=[edges, edges])
    return h.astype(np.float32)


def _centred(tracks: pd.DataFrame, info: dict, node: str) -> tuple[pd.DataFrame, dict]:
    """Node track relative to the clicked bowl centre, and the rim in the same frame."""
    cx, cy = info["center"]
    out = tracks.loc[:, [f"{node}_x", f"{node}_y", "valid"]].copy()
    out[f"{node}_x"] -= cx
    out[f"{node}_y"] -= cy
    return out, shift_rim(info["rim"], -cx, -cy)


def _session_order(s: str) -> tuple:
    try:
        return (0, int(s))
    except (TypeError, ValueError):
        return (1, str(s))


def _per_animal_figures(cfg: Config, meta: pd.DataFrame, results: dict, out_dir: Path, log: Log) -> None:
    """For every animal x context: a bowl-centred heatmap and tornado plots with one panel per
    session the animal has (in recording order; parts of a session are joined)."""
    node = cfg.bouts.node
    for (animal, ctx), sub in meta.groupby(["animal", "context"], dropna=False):
        sub = sub[sub["video"].isin(results)]
        panels = []
        for session, ses in sorted(sub.groupby("session", dropna=False), key=lambda kv: _session_order(kv[0])):
            vids = ses.sort_values("part", na_position="first")["video"].tolist()
            parts = [_centred(results[v]["tracks"], results[v]["info"], node) for v in vids]
            cond = ses["condition"].iloc[0]
            panels.append((pd.concat([p[0] for p in parts], ignore_index=True), parts[0][1],
                           f"{cond} {ctx}  ({'+'.join(vids)})"))
        if not panels:
            continue
        group = sub["group"].iloc[0]
        who = f"{group} #{animal}" if isinstance(group, str) and group else f"#{animal}"
        panels = [(t, rim, f"{who}, {title}") for t, rim, title in panels]
        stem = f"animal{animal}_{ctx}"
        log(f"figures: {stem} ({len(panels)} session{'s' if len(panels) > 1 else ''})")
        plots.session_heatmaps(panels, node, out_dir / "heatmaps" / f"{stem}.png")
        for color_by in ("distance", "speed"):
            plots.session_tornados(panels, node, out_dir / f"tornado_{color_by}" / f"{stem}.png", color_by)


def _animal_boxplots(feats: pd.DataFrame, cfg: Config, out_dir: Path) -> None:
    """Every bout feature, per animal and condition."""
    from .stats import feature_columns

    feats = feats.copy()
    by = "condition"
    if feats["context"].nunique() > 1:  # one box per condition in each chamber
        by = "condition_context"
        feats[by] = feats["condition"].astype(str) + " " + feats["context"].astype(str)
    sessions = feats.drop_duplicates("video")
    cells = sorted(sessions[by].dropna().unique(),
                   key=lambda c: min(_session_order(s) for s in sessions.loc[sessions[by] == c, "session"]))
    for f in [c for c in feature_columns(feats)]:
        plots.animal_boxplot(feats, f, cells, out_dir / f"{f}.png", by=by)


def _exploratory(feats: pd.DataFrame, a, conds: list[str], node: str, ex: Path, make_plots: bool) -> None:
    """PCA / UMAP projections, local same-condition clustering and a random forest (first vs last condition)."""
    pcols = exploratory.pca_features(node)
    coords, kept, pinfo = exploratory.project(feats, "PCA", a.seed, pcols)
    cols = [c for c in ("video", "bout", "group", "condition", "context") if c in kept]
    pd.concat([kept[cols].reset_index(drop=True), pd.DataFrame(coords, columns=["pc1", "pc2"])], axis=1).to_csv(ex / "pca.csv", index=False)
    ex_summary = {"pca": pinfo}
    if make_plots:
        plots.projection_scatter(coords, kept, "PCA", ex)
    try:
        ucoords, ukept, _ = exploratory.project(feats, "UMAP", a.seed, pcols)
        pd.concat([ukept[cols].reset_index(drop=True), pd.DataFrame(ucoords, columns=["umap1", "umap2"])], axis=1).to_csv(ex / "umap.csv", index=False)
        if make_plots:
            plots.projection_scatter(ucoords, ukept, "UMAP", ex)
    except ImportError:
        ex_summary["umap"] = "skipped (install the 'umap' extra)"
    kept = kept.reset_index(drop=True)
    for label in dict.fromkeys((conds[-1], conds[0])):
        clus = exploratory.local_condition_clustering(coords, kept, label)
        test = exploratory.clustering_test(clus)
        clus.to_csv(ex / f"local_clustering_{label}.csv", index=False)
        ex_summary[f"local_clustering_{label}"] = test
        if make_plots and test:
            plots.local_clustering_box(clus, test, label, 31, ex / f"local_clustering_{label}.png")
    rf = exploratory.condition_classifier(feats, [conds[0], conds[-1]], a.seed)
    rf.to_csv(ex / "rf_conditions.csv", index=False)
    ex_summary["rf_conditions"] = [conds[0], conds[-1]]
    ex_summary["rf_auc"] = rf.groupby("context")["auc"].first().to_dict() if len(rf) else {}
    if make_plots:
        plots.rf_importance(rf, ex / "rf_importance.png")
    write_json(ex / "summary.json", ex_summary)


def run_analysis(cfg: Config, videos: list[str] | None = None, make_plots: bool = True, log: Log = print) -> Path:
    t0 = time.time()
    status = video_status(cfg)
    if videos:
        status = status[status["video"].isin(videos)]
    stem = f"{datetime.now():%Y%m%d-%H%M%S}_{cfg.analysis_hash()}"
    run_dir, k = cfg.paths.results / stem, 2
    while run_dir.exists():  # two runs within one second
        run_dir, k = cfg.paths.results / f"{stem}-{k}", k + 1
    run_dir.mkdir(parents=True)
    log(f"run directory: {run_dir}")

    results, skipped, inputs = {}, {}, []
    for row in status.itertuples(index=False):
        missing = missing_inputs(cfg, row.video)
        if missing:
            skipped[row.video] = "missing " + ", ".join(missing)
            continue
        log(f"processing {row.video}")
        try:
            results[row.video] = process_video(cfg, row.video, Path(row.path))
        except Exception as exc:  # keep going; record why
            skipped[row.video] = f"error: {exc}"
            log(f"  ! {row.video}: {exc}")
            continue
        prov_p = prediction_paths(cfg, row.video)["provenance"]
        prov = json.loads(prov_p.read_text(encoding="utf-8")) if prov_p.exists() else {}
        inputs.append({
            "video": row.video,
            "predictions_sha256": results[row.video]["info"]["key"]["predictions_sha256"],
            "predictions_source": prov.get("source", "unknown"),
            "model": prov.get("model"),
            "sleap_version": prov.get("sleap_version"),
            "bowl": load_bowl(cfg.paths.bowls, row.video).model_dump(mode="json"),
            "rim": results[row.video]["info"]["rim"],
        })

    meta = status[status["video"].isin(results)].drop(columns=["path", "has_predictions", "has_bowl"])
    bouts = pd.concat([r["bouts"].assign(video=v) for v, r in results.items()], ignore_index=True) if results else pd.DataFrame()
    feats = pd.concat([r["features"].assign(video=v) for v, r in results.items()], ignore_index=True) if results else pd.DataFrame()
    sessions = pd.DataFrame([{"video": v, **r["summary"]} for v, r in results.items()])
    outputs = []
    if len(feats):
        feats = meta.merge(feats, on="video")
        bouts.to_csv(run_dir / "bouts.csv", index=False)
        feats.to_csv(run_dir / "bout_features.csv", index=False)
        outputs += ["bouts.csv", "bout_features.csv"]
    if len(sessions):
        sessions = meta.merge(sessions, on="video")
        sessions.to_csv(run_dir / "sessions.csv", index=False)
        outputs.append("sessions.csv")

    a = cfg.analysis
    node = cfg.bouts.node
    if results:
        np.savez_compressed(run_dir / "occupancy.npz", __extent__=np.float32(OCC_EXTENT),
                            **{v: occupancy(r["tracks"], r["info"], node) for v, r in results.items()})
        write_json(run_dir / "occupancy_rims.json",
                   {v: shift_rim(r["info"]["rim"], -r["info"]["center"][0], -r["info"]["center"][1]) for v, r in results.items()})
        outputs.append("occupancy.npz")

    notes = []  # what could not be computed, and why (shown in the UI)
    if len(feats):
        log("comparison views")
        views = run_views(run_dir, cfg, make_plots=make_plots, log=log)
        outputs.append("views/")
        for v in views:
            if v.get("error"):
                log(f"  ! view {v['name']}: {v['error']}")
        if not views:
            notes.append("No comparisons: the built-in one needs animals in at least two groups (Settings > Animals "
                         "and groups) and both analysed conditions; or add your own under Results > Comparisons.")

        conds = analysis_conditions(a.conditions, feats)
        sub_feats = feats[feats["condition"].isin(conds)]
        ex = run_dir / "exploratory"
        ex.mkdir(exist_ok=True)
        if len(sub_feats) >= 10 and conds:
            log("exploratory analyses")
            try:
                _exploratory(sub_feats, a, conds, node, ex, make_plots)
                outputs.append("exploratory/")
            except Exception as exc:  # small or one-sided data; the rest of the run is still useful
                log(f"  ! exploratory analyses: {exc}")
                notes.append(f"Exploratory analyses failed: {exc}")
        else:
            notes.append(f"No exploratory analyses: they need at least 10 bouts from the conditions "
                         f"{', '.join(conds) or '(none found)'} (this run has {len(sub_feats)}).")

        if make_plots:
            log("figures: bout features per animal")
            _animal_boxplots(feats, cfg, run_dir / "exploratory" / "per_animal")
    elif results:
        notes.append("No bouts were found, so there is nothing to compare. Check the bowl annotations and tracking.")

    if make_plots and results:
        _per_animal_figures(cfg, meta, results, run_dir / "figures", log)
        outputs.append("figures/")

    shutil.copy2(cfg.source, run_dir / "feeding.yaml")
    write_json(run_dir / "manifest.json", {
        "created_at": now_iso(),
        "elapsed_s": round(time.time() - t0, 1),
        "project": {"name": cfg.project_name, "root": str(cfg.root)},
        "analysis_hash": cfg.analysis_hash(),
        "config": cfg.model_dump(mode="json"),
        "subjects_sha256": sha256(cfg.paths.subjects),
        "git": git_state(),
        "packages": package_versions(),
        "n_videos_analysed": len(results),
        "n_bouts": int(len(feats)),
        "inputs": inputs,
        "skipped": skipped,
        "notes": notes,
        "outputs": outputs,
    })
    log(f"done: {len(results)} videos, {len(feats)} bouts, {len(skipped)} skipped -> {run_dir}")
    return run_dir
