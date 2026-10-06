import json

import pytest
import yaml

from feeding.config import load_project
from feeding.pipeline import video_status
from feeding.project import init_project, parse_model_args


def _fake_model(d, nodes):
    d.mkdir(parents=True)
    node_objs = [{"id": {"py/object": "sleap.skeleton.Node", "py/state": {"py/tuple": [n, 1.0]}}} for n in nodes]
    (d / "training_config.json").write_text(json.dumps({
        "data": {"labels": {"skeletons": [{"links": [], "nodes": node_objs}]}},
        "model": {"heads": {"single_instance": {"part_names": nodes}, "centroid": None}},
    }))
    return d


def test_parse_model_args(tmp_path):
    m = _fake_model(tmp_path / "models" / "run.single_instance.n=12", ["Nose"])
    assert parse_model_args([str(m)]) == {"default": [m]}
    assert parse_model_args([f"A={m}", f"B={m}", f"B={m}"]) == {"A": [m], "B": [m, m]}
    assert parse_model_args([str(m / "training_config.json")]) == {"default": [m]}


def test_init_project_with_custom_naming_and_skeleton(tmp_path):
    videos = tmp_path / "vids"
    videos.mkdir()
    for name in ["ratA_day1_box2", "ratB_day2_box1", "notes"]:
        (videos / f"{name}.mp4").touch()
    model = _fake_model(tmp_path / "m" / "x.n=5", ["Nose", "Head", "Tail"])
    pattern = r"^(?P<animal>rat\w)_(?P<condition>day\d)_(?P<context>box\d)$"

    cfg_path, warnings = init_project(tmp_path / "proj", {"default": [model]}, videos,
                                      tmp_path / "no-sleap-env", pattern)
    cfg = load_project(tmp_path / "proj")
    assert cfg.project_name == "proj" and cfg.root == tmp_path / "proj"
    assert cfg.skeleton.nodes == ["Nose", "Head", "Tail"]
    assert cfg.bouts.node == "Nose" and cfg.quality.drop_nodes == []  # "Snout"/"TipTail" don't exist here
    assert cfg.sleap.model_dirs("box9") == [model]
    st = video_status(cfg)
    assert sorted(st.video) == ["ratA_day1_box2", "ratB_day2_box1"]  # "notes" ignored
    assert set(st.animal) == {"ratA", "ratB"}
    subjects = (tmp_path / "proj" / "metadata" / "subjects.csv").read_text().split()
    assert subjects[0] == "animal,group" and len(subjects) == 3
    assert any("bouts.node" in w for w in warnings)
    from feeding.project import check_project

    issues = {i["message"].split(":")[0] for i in check_project(cfg)}
    assert any("no group" in m for m in issues)  # subjects.csv groups are blank until filled in

    with pytest.raises(FileExistsError):
        init_project(tmp_path / "proj", {"default": [model]}, videos, tmp_path, pattern)
    with pytest.raises(ValueError, match="no video"):
        init_project(tmp_path / "p2", {"default": [model]}, videos, tmp_path, r"^(?P<animal>mouse\d+)$")
    with pytest.raises(ValueError, match="no model for contexts"):
        init_project(tmp_path / "p3", {"box2": [model]}, videos, tmp_path, pattern)


def test_minimal_project_and_discovery(tmp_path, monkeypatch):
    from feeding.config import NoProjectError, find_project

    model = _fake_model(tmp_path / "m" / "x", ["Snout", "Tail"])
    proj = tmp_path / "minimal"
    (proj / "sub" / "deeper").mkdir(parents=True)
    (proj / "feeding.yaml").write_text(f"sleap:\n  models:\n    default: {model}\n")
    cfg = load_project(proj)
    assert cfg.skeleton.nodes == ["Snout", "Tail"]  # read from the model
    assert cfg.paths.videos == [proj / "videos"] and cfg.features.speed_nodes == ["Snout", "Tail"]
    monkeypatch.delenv("FEEDING_PROJECT", raising=False)
    monkeypatch.chdir(proj / "sub" / "deeper")
    assert find_project() == proj / "feeding.yaml"  # found from a sub-folder
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NoProjectError):
        find_project()


def test_config_rejects_unknown_nodes(tmp_path):
    raw = {"skeleton": {"nodes": ["Snout", "Ear", "Tail"]}, "bouts": {"node": "Nose"}}
    p = tmp_path / "feeding.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="bouts.node"):
        load_project(p)


def test_barebones_project_grows(tmp_path, monkeypatch):
    """Create from a single uploaded video, then add a folder, a model and groups."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("FEEDING_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("FEEDING_MODEL_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("FEEDING_RECENT_FILE", str(tmp_path / "recent.json"))
    monkeypatch.delenv("FEEDING_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    lib_model = _fake_model(tmp_path / "library" / "mouse.n=10", ["Snout", "Ear", "Tail"])
    import importlib

    import feeding.server.app as appmod

    appmod = importlib.reload(appmod)
    c = TestClient(appmod.app)
    assert not c.get("/api/project").json()["open"]

    r = c.post("/api/project/new", json={"name": "My cohort", "video_names": ["m1_pre.mp4"]})
    assert r.status_code == 200, r.text
    p = r.json()
    root = tmp_path / "projects" / "My-cohort"
    assert p["open"] and p["root"] == str(root) and p["name"] == "My cohort"
    assert any("no SLEAP model" in n for n in p["notes"])
    up = c.put("/api/project/videos/upload?name=m1_pre.mp4", content=b"\x00" * 1000).json()
    assert up["matches_pattern"] and (root / "videos" / "m1_pre.mp4").stat().st_size == 1000
    assert c.put("/api/project/videos/upload?name=m1_pre.mp4", content=b"x").status_code == 409
    assert c.put("/api/project/videos/upload?name=notes.txt", content=b"x").status_code == 422

    # A second folder of videos, used in place.
    more = tmp_path / "more"
    more.mkdir()
    (more / "m2_post.mp4").write_bytes(b"\x00")
    s = c.post("/api/project/videos/folder", json={"path": str(more)}).json()
    assert [d["path"] for d in s["video_dirs"]] == [str(root / "videos"), str(more)]

    # Naming: the guessed generic pattern makes the whole name the animal; switch to animal_condition.
    prev = c.get("/api/project/naming-preview", params={"pattern": r"^(?P<animal>[^_]+)_(?P<condition>[^_]+)$"}).json()
    assert prev["matched"] == 2 and {r["animal"] for r in prev["rows"]} == {"m1", "m2"}
    assert "error" in c.get("/api/project/naming-preview", params={"pattern": "(?P<animal>"}).json()

    # Model from the library; skeleton follows; groups saved.
    s = c.get("/api/project/settings").json()
    assert [m["path"] for m in s["available_models"]] == [str(lib_model)]
    s = c.put("/api/project/settings", json={
        "pattern": r"^(?P<animal>[^_]+)_(?P<condition>[^_]+)$", "conditions": ["pre", "post"],
        "models": {"default": [str(lib_model)]},
        "subjects": [{"animal": "m1", "group": "Control"}, {"animal": "m2", "group": "Treated"}],
    }).json()
    assert s["skeleton"] == ["Snout", "Ear", "Tail"] and s["models"]["default"] == [str(lib_model)]
    assert {r["animal"]: r["group"] for r in s["subjects"]} == {"m1": "Control", "m2": "Treated"}
    cfg = load_project(root)
    assert cfg.naming.pattern.startswith("^(?P<animal>[^_]+)") and cfg.analysis.conditions == ["pre", "post"]
    text = (root / "feeding.yaml").read_text()
    assert "# Feeding-behaviour project configuration." in text  # comments preserved by the editor
    # SLEAP inference parameters: described for the form, saved to feeding.yaml, validated.
    s = c.get("/api/project/settings").json()
    info = {p["name"]: p for p in s["sleap_param_info"]}
    assert info["tracker"]["choices"] == ["none", "simple", "flow"] and info["batch_size"]["default"] == 4
    assert s["sleap_params"]["peak_threshold"] == 0.2
    s = c.put("/api/project/settings", json={"sleap_params": {"batch_size": 8, "tracker": "simple", "device": "cpu"}}).json()
    assert s["sleap_params"]["batch_size"] == 8 and load_project(root).sleap.tracker == "simple"
    assert c.put("/api/project/settings", json={"sleap_params": {"batch_size": 0}}).status_code == 422
    assert c.put("/api/project/settings", json={"sleap_params": {"bogus": 1}}).status_code == 422
    text = (root / "feeding.yaml").read_text()
    # Bad settings are rejected and the file is left intact.
    assert c.put("/api/project/settings", json={"pattern": "(?P<x>.*)"}).status_code == 422
    assert (root / "feeding.yaml").read_text() == text


def test_native_pick_and_copy_videos(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import feeding.native_dialogs as nd

    monkeypatch.setenv("FEEDING_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("FEEDING_MODEL_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("FEEDING_RECENT_FILE", str(tmp_path / "recent.json"))
    monkeypatch.delenv("FEEDING_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path)
    import importlib

    import feeding.server.app as appmod

    appmod = importlib.reload(appmod)
    src = tmp_path / "camera"
    src.mkdir()
    for n in ("m1_pre.mp4", "m2_pre.mp4"):
        (src / n).write_bytes(b"\x00" * 10)
    monkeypatch.setattr(nd, "pick", lambda kind, title, start: [str(src / "m1_pre.mp4"), str(src / "m2_pre.mp4")])

    remote = TestClient(appmod.app)  # client host "testclient": not this machine
    assert remote.post("/api/native/pick", json={"kind": "videos"}).json()["supported"] is False
    local = TestClient(appmod.app, client=("127.0.0.1", 50000))
    picked = local.post("/api/native/pick", json={"kind": "videos"}).json()
    assert picked["supported"] and len(picked["paths"]) == 2
    monkeypatch.setenv("FEEDING_NATIVE_DIALOGS", "0")
    assert local.post("/api/native/pick", json={"kind": "folder"}).json()["supported"] is False

    # Create a project straight from picked files (copied, originals untouched), then add another.
    r = local.post("/api/project/new", json={"name": "picked", "video_files": [picked["paths"][0]]})
    assert r.status_code == 200, r.text
    root = tmp_path / "projects" / "picked"
    assert (root / "videos" / "m1_pre.mp4").exists() and (src / "m1_pre.mp4").exists()
    s = local.post("/api/project/videos/add", json={"paths": [picked["paths"][1]]}).json()
    assert len(s["added"]) == 1 and (root / "videos" / "m2_pre.mp4").exists()
    assert local.post("/api/project/videos/add", json={"paths": [picked["paths"][1]]}).status_code == 409
    assert len(local.get("/api/videos").json()) == 2


def test_sleap_track_args():
    from feeding.config import SleapConfig

    s = SleapConfig()
    assert s.track_args() == ["--batch_size", "4", "--peak_threshold", "0.2", "--tracking.tracker", "none"]
    s = SleapConfig(batch_size=16, device="1", tracker="flow", target_instance_count=2, extra_args=["--tracking.match", "hungarian"])
    a = s.track_args()
    assert a[a.index("--gpu") + 1] == "1" and a[a.index("--tracking.target_instance_count") + 1] == "2"
    assert a.count("--tracking.match") == 1 and a[-2:] == ["--tracking.match", "hungarian"]  # extra args win
    assert "--first-gpu" not in SleapConfig(device="cpu", extra_args=["--first-gpu"]).track_args()[:-1]
    with pytest.raises(ValueError):
        SleapConfig(device="tpu")
    with pytest.raises(ValueError):
        SleapConfig(tracker="kalman")


def test_demo_project(tmp_path):
    from feeding.demo import make_demo
    from feeding.pipeline import run_analysis

    cfg_path = make_demo(tmp_path / "demo", animals_per_group=2, seconds=40, log=lambda s: None)
    cfg = load_project(cfg_path)
    st = video_status(cfg)
    assert len(st) == 16 and st.has_predictions.all() and st.has_bowl.all()
    assert set(st.group) == {"Control", "Treated"} and cfg.analysis.conditions == ["Pre", "Post"]
    assert cfg.naming.chambers == {"A": "square arena", "B": "round arena"} and cfg.quality.drop_nodes == ["TailTip"]
    out = run_analysis(cfg, make_plots=False, log=lambda s: None)
    assert json.loads((out / "manifest.json").read_text())["n_bouts"] >= 8
    with pytest.raises(FileExistsError):
        make_demo(tmp_path / "demo", log=lambda s: None)


def test_manual_and_help_api():
    import importlib

    from fastapi.testclient import TestClient

    from feeding import docs

    chs = docs.chapters()
    assert chs[0]["slug"] == "introduction" and len(chs) >= 10
    files = {c["file"] for c in chs}
    ids = {c["slug"]: {t["id"] for t in docs.chapter(c["slug"])["toc"]} for c in chs}
    # every link between chapters points at an existing chapter and heading; every image exists
    import re

    for c in chs:
        text = (docs.docs_dir() / c["file"]).read_text()
        for file, anchor in re.findall(r"\]\(([0-9]+-[a-z0-9-]+\.md)(?:#([^)]+))?\)", text):
            assert file in files, (c["file"], file)
            slug = next(x["slug"] for x in chs if x["file"] == file)
            assert not anchor or anchor in ids[slug] | {slug}, (c["file"], file, anchor)
        for img in re.findall(r"\]\((images/[^)]+)\)", text):
            assert (docs.docs_dir() / img).exists(), (c["file"], img)

    import feeding.server.app as appmod

    c = TestClient(importlib.reload(appmod).app)
    listing = c.get("/api/docs").json()
    assert [x["slug"] for x in listing] == [x["slug"] for x in chs]
    d = c.get("/api/docs/sleap").json()
    assert 'href="#help/installation/' in d["html"] and 'src="/docs/manual/images/' in d["html"]
    assert any(t["id"] == "inference-parameters" for t in d["toc"])
    hits = c.get("/api/docs/search", params={"q": "peak threshold"}).json()
    assert hits[0]["slug"] == "sleap" and hits[0]["anchor"] == "inference-parameters"
    assert c.get("/api/docs/nope").status_code == 404
    assert c.get("/docs/manual/images/main-window.png").status_code == 200
    assert c.get("/docs/manual/../../pyproject.toml").status_code == 404
