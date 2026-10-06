"""End-to-end test on a synthetic project: generated videos + SLEAP-format analysis files."""

import importlib
import json

import cv2
import h5py
import numpy as np
import pandas as pd
import pytest
import yaml

from feeding.bowl import BowlAnnotation, save_bowl
from feeding.config import load_project

NODES = ["Snout", "LeftEar", "RightEar", "StartTail", "TipTail", "MidBack"]
W, H, T = 96, 72, 900
BOWL = (48.0, 36.0, 6.0)  # cx, cy, radius
VIDEOS = ["1_Pre_A_1", "2_Post_A_1", "1_Pre_A_6", "2_Post_A_6", "3_Pre_A_2", "4_Post_A_2", "3_Pre_A_7", "4_Post_A_7"]


def _trajectory(seed: int) -> np.ndarray:
    """Snout path that visits the bowl every ~150 frames for ~40 frames."""
    rng = np.random.default_rng(seed)
    t = np.arange(T)
    phase = (t % 150) / 150
    r = np.where((phase > 0.4) & (phase < 0.4 + 40 / 150), 2.0, 25.0 + 5 * np.sin(t / 7))
    ang = t / 40 + seed
    xy = np.column_stack([BOWL[0] + r * np.cos(ang), BOWL[1] + r * np.sin(ang)])
    return xy + rng.normal(0, 0.3, xy.shape)


def _write_video(path, n):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (W, H))
    for i in range(n):
        frame = np.full((H, W, 3), 40, np.uint8)
        cv2.circle(frame, (int(BOWL[0]), int(BOWL[1])), int(BOWL[2]), (230, 230, 230), -1)
        cv2.putText(frame, str(i % 100), (2, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255))
        vw.write(frame)
    vw.release()


def _write_analysis(path, snout):
    tracks = np.zeros((1, 2, len(NODES), T))
    for j in range(len(NODES)):
        tracks[0, 0, j] = snout[:, 0] + 3 * j
        tracks[0, 1, j] = snout[:, 1]
    scores = np.full((1, len(NODES), T), 0.9)
    tracks[0, :, :, 400:420] = np.nan  # a tracking dropout
    scores[0, :, 400:420] = np.nan
    with h5py.File(path, "w") as f:
        f["tracks"] = tracks
        f["point_scores"] = scores
        f["node_names"] = np.array([n.encode() for n in NODES])


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    (root / "videos").mkdir()
    (root / "data" / "predictions").mkdir(parents=True)
    for i, v in enumerate(VIDEOS):
        _write_video(root / "videos" / f"{v}.mp4", T)
        _write_analysis(root / "data" / "predictions" / f"{v}.analysis.h5", _trajectory(i))
    pd.DataFrame({"animal": [1, 2, 6, 7], "group": ["Control", "Control", "Treated", "Treated"]}).to_csv(root / "subjects.csv", index=False)
    cfg = {
        "paths": {"videos": "videos", "predictions": "data/predictions", "bowls": "bowls", "subjects": "subjects.csv",
                  "derived": "data/derived", "results": "data/results", "logs": "data/logs"},
        "sleap": {"bin_dir": "/nonexistent", "models": {"A": "/nonexistent", "B": "/nonexistent"}},
        "skeleton": {"nodes": NODES, "edges": [["Snout", "LeftEar"]]},
        "quality": {"drop_nodes": ["TipTail"], "score_window": 10, "min_score": 0.5, "min_segment_frames": 30},
        "bouts": {"min_contact_frames": 10, "merge_gap_frames": 20, "flank_frames": 20},
        "features": {"speed_nodes": ["Snout", "MidBack"]},
        "analysis": {"unit": "animal"},
    }
    (root / "feeding.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    config = load_project(root)
    cx, cy, r = BOWL
    for v in VIDEOS:
        save_bowl(config.paths.bowls, BowlAnnotation(video=v, center=(cx, cy), top=(cx, cy - r), bottom=(cx, cy + r),
                                                     left=(cx - r, cy), right=(cx + r, cy)))
    return root, config


def test_pipeline(project):
    from feeding.pipeline import run_analysis

    _, cfg = project
    out = run_analysis(cfg, make_plots=False, log=lambda s: None)
    feats = pd.read_csv(out / "bout_features.csv")
    sessions = pd.read_csv(out / "sessions.csv")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert len(sessions) == len(VIDEOS)
    # ~6 bowl visits per video; the dropout and edges remove a couple.
    assert 3 <= feats.groupby("video").size().min() <= 6
    assert set(feats["group"]) == {"Control", "Treated"}
    assert manifest["n_videos_analysed"] == len(VIDEOS) and not manifest["skipped"]
    ex = json.loads((out / "exploratory" / "summary.json").read_text(encoding="utf-8"))  # conditions default to all, by session
    assert ex["rf_conditions"] == ["Pre", "Post"] and (out / "exploratory" / "rf_conditions.csv").exists()
    stats = pd.read_csv(out / "views" / "standard" / "stats_bouts.csv")
    tested = stats.dropna(subset=["pvalue"])["pair"].unique()
    assert sorted(tested) == sorted(  # only context A has data in this project
        ["Control Pre A vs Treated Pre A", "Control Post A vs Treated Post A", "Control Pre A vs Control Post A", "Treated Pre A vs Treated Post A"])
    assert stats.loc[stats["pair"] == "Control Pre A vs Control Post A", "paired"].all()  # same mice before and after
    assert not stats.loc[stats["pair"] == "Control Pre A vs Treated Pre A", "paired"].any()
    sessions = pd.read_csv(out / "views" / "standard" / "stats_sessions.csv")
    assert {"MidBack_distance_px", "MidBack_speed_mean"} <= set(sessions["feature"])
    # A run with a single video still completes and says why there are no statistics.
    one = run_analysis(cfg, videos=[VIDEOS[0]], make_plots=True, log=lambda s: None)
    m1 = json.loads((one / "manifest.json").read_text(encoding="utf-8"))
    assert m1["n_videos_analysed"] == 1 and any("two groups" in n for n in m1["notes"])
    assert (one / "sessions.csv").exists()
    # ...and still draws the per-animal figures and per-animal box plots.
    assert len(list((one / "figures" / "heatmaps").glob("animal*.png"))) == 1
    assert len(list((one / "figures" / "tornado_speed").glob("animal*.png"))) == 1
    assert (one / "exploratory" / "per_animal" / "num_returns.png").exists()

    # A custom all-pairs view, re-run on the finished run with figures.
    from feeding.config import View
    from feeding.views import run_views

    view = View(name="Control vs Treated, all sessions", groups=[
        {"label": "Control", "where": {"group": "Control"}}, {"label": "Treated", "where": {"group": "Treated"}}])
    (summary,) = run_views(out, cfg, [view], make_plots=True, log=lambda s: None)
    assert summary["slug"] == "control-vs-treated-all-sessions" and "error" not in summary
    vdir = out / "views" / summary["slug"]
    st = pd.read_csv(vdir / "stats_bouts.csv")
    assert set(st["pair"]) == {"Control vs Treated"} and not st["paired"].any()
    figs = vdir / "figures"
    assert (figs / "overview_bouts.png").exists() and (figs / "occupancy.png").exists() and (figs / "pca.png").exists()
    assert (figs / "boxplots_sessions" / "MidBack_speed_mean.png").exists()
    index = json.loads((out / "views" / "index.json").read_text(encoding="utf-8"))
    assert [v["slug"] for v in index] == ["standard", summary["slug"]]
    # Cached tracks are reused and invalidated when the bowl changes.
    from feeding.tracks import load_tracks

    _, info1 = load_tracks(cfg, VIDEOS[0], cfg.paths.videos[0] / f"{VIDEOS[0]}.mp4")
    ann = BowlAnnotation.model_validate_json((cfg.paths.bowls / f"{VIDEOS[0]}.json").read_text(encoding="utf-8"))
    save_bowl(cfg.paths.bowls, ann.model_copy(update={"top": (48.0, 28.0)}))
    _, info2 = load_tracks(cfg, VIDEOS[0], cfg.paths.videos[0] / f"{VIDEOS[0]}.mp4")
    assert info1["key"] != info2["key"]
    save_bowl(cfg.paths.bowls, ann)


def test_api(project, monkeypatch):
    from fastapi.testclient import TestClient

    root, _ = project
    monkeypatch.setenv("FEEDING_PROJECT", str(root))
    monkeypatch.setenv("FEEDING_RECENT_FILE", str(root / "recent.json"))
    import feeding.server.app as appmod

    appmod = importlib.reload(appmod)
    c = TestClient(appmod.app)

    vids = c.get("/api/videos").json()
    assert len(vids) == len(VIDEOS) and all(v["has_predictions"] and v["has_bowl"] for v in vids)
    v = VIDEOS[0]
    d = c.get(f"/api/videos/{v}").json()
    assert d["props"]["n_frames"] == T and d["bowl"]["rim"]["semi_major"] == pytest.approx(BOWL[2])
    r = c.get(f"/api/videos/{v}/frame/10")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/videos/{v}/frame/{T}").status_code == 404
    p = c.get(f"/api/videos/{v}/poses?start=0&count=200").json()
    assert p["count"] == 200 and any(p["in_bowl"])
    tl = c.get(f"/api/videos/{v}/timeline?bins=100").json()
    assert len(tl["valid"]) == 100 and tl["summary"]["n_bouts"] >= 3

    body = {"center": [48, 36], "top": [48, 29], "bottom": [48, 43], "left": [40, 36], "right": [56, 36], "frame_idx": 5}
    saved = c.put(f"/api/videos/{v}/bowl", json=body).json()
    assert saved["rim"]["semi_major"] == pytest.approx(8) and saved["frame_idx"] == 5
    poly = {"shape": "polygon", "center": [48, 36], "polygon": [[40, 30], [56, 30], [56, 42], [40, 42]]}
    saved = c.put(f"/api/videos/{v}/bowl", json=poly).json()
    assert saved["rim"]["type"] == "polygon" and saved["rim"]["area_px2"] == pytest.approx(16 * 12)
    tl = c.get(f"/api/videos/{v}/timeline?bins=100").json()  # tracks recomputed with the polygon
    assert tl["summary"]["in_bowl_frames"] > 0
    assert c.put(f"/api/videos/{v}/bowl", json=dict(poly, polygon=[[40, 30], [56, 30]])).status_code == 422
    bad = dict(body, top=[48, 36], bottom=[48, 36])
    assert c.put(f"/api/videos/{v}/bowl", json=bad).status_code == 422
    assert c.get("/api/videos/not_a_video").status_code == 404

    # Projects: info, close, folder browser, open again.
    p = c.get("/api/project").json()
    assert p["open"] and p["root"] == str(root) and p["nodes"] == NODES
    assert any("SLEAP model folder not found" in i["message"] for i in p["issues"])
    assert not c.post("/api/project/close").json()["open"]
    assert c.get("/api/videos").status_code == 409
    fs = c.get(f"/api/fs?path={root.parent}").json()
    assert any(e["is_project"] and e["path"] == str(root) for e in fs["entries"])
    assert c.post("/api/project/open", json={"path": str(root / "videos")}).status_code == 404
    assert c.post("/api/project/open", json={"path": str(root)}).json()["name"] == root.name
    assert any(r["path"] == str(root) for r in c.get("/api/project").json()["recent"])


def test_results_and_views_api(project, monkeypatch):
    import io
    import time
    import zipfile

    from fastapi.testclient import TestClient

    root, cfg = project
    monkeypatch.setenv("FEEDING_PROJECT", str(root))
    monkeypatch.setenv("FEEDING_RECENT_FILE", str(root / "recent.json"))
    if not list(cfg.paths.results.glob("*/manifest.json")):
        from feeding.pipeline import run_analysis

        run_analysis(cfg, make_plots=False, log=lambda s: None)
    import feeding.server.app as appmod

    appmod = importlib.reload(appmod)
    c = TestClient(appmod.app)

    v = c.get("/api/views").json()
    assert v["standard"]["name"] == "Standard" and v["standard_enabled"]
    assert set(v["fields"]["group"]) == {"Control", "Treated"}
    view = {"name": "Control: Pre vs Post", "groups": [
        {"label": "Pre", "where": {"group": ["Control"], "condition": ["Pre"]}},
        {"label": "Post", "where": {"group": ["Control"], "condition": ["Post"]}}]}
    assert c.put("/api/views", json={"views": [view, view]}).status_code == 422  # duplicate names
    never = c.put("/api/views", json={"views": [dict(view, name="never paired", paired="no")]})
    assert never.status_code == 200 and never.json()["views"][0]["paired"] == "no"  # YAML 'no' is not False
    assert c.put("/api/views", json={"views": [dict(view, groups=view["groups"][:1])]}).status_code == 422
    v = c.put("/api/views", json={"views": [view]}).json()
    assert [x["name"] for x in v["views"]] == ["Control: Pre vs Post"]
    assert "Control: Pre vs Post" in (root / "feeding.yaml").read_text(encoding="utf-8")

    run = next(r["name"] for r in c.get("/api/results").json() if r["n_videos"] == len(VIDEOS))
    d = c.get(f"/api/results/{run}").json()
    assert d["n_videos"] == len(VIDEOS) and [x["slug"] for x in d["views"]][0] == "standard"
    assert c.get("/api/results/nope").status_code == 404
    std = c.get(f"/api/results/{run}/views/standard").json()
    assert std["stats_bouts"] and std["groups"][0]["animals"] >= 1

    job = c.post(f"/api/results/{run}/views", json={"slugs": ["control-pre-vs-post"], "plots": False}).json()["submitted"][0]
    for _ in range(200):
        st = c.get(f"/api/jobs/{job}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st.get("log")
    lv = c.get(f"/api/results/{run}/views/control-pre-vs-post").json()
    assert {r["pair"] for r in lv["stats_bouts"]} == {"Pre vs Post"} and all(r["paired"] for r in lv["stats_bouts"])
    assert c.post(f"/api/results/{run}/views", json={"slugs": ["missing"]}).status_code == 422

    z = c.get(f"/api/results/{run}/export.zip?sub=views/control-pre-vs-post")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert f"{run}/views/control-pre-vs-post/stats.xlsx" in names and not any("/standard/" in n for n in names)
    full = zipfile.ZipFile(io.BytesIO(c.get(f"/api/results/{run}/export.zip").content)).namelist()
    assert f"{run}/manifest.json" in full and f"{run}/bout_features.csv" in full
    assert c.get(f"/api/results/{run}/export.zip?sub=../..").status_code == 404
    c.put("/api/views", json={"views": []})
