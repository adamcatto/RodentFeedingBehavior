"""FastAPI backend for the browser UI (``uv run feeding serve [PROJECT]``).

The server works on one *open project* at a time (a folder with feeding.yaml).
It starts with the project given on the command line (or found from the
current folder), or with none, in which case the UI asks to open or create one.
Background jobs keep a reference to the project they were started in, so
switching projects while SLEAP runs is safe.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..bouts import detect_bouts
from ..bowl import BowlAnnotation, delete_bowl, load_bowl, save_bowl
from ..config import (
    DEFAULT_NAME_PATTERN, PROJECT_CONFIG_NAME, SLEAP_PARAMS, Config, NoProjectError, SleapConfig, load_project,
)
from ..naming import VIDEO_EXTENSIONS, parse_video_name
from ..pipeline import run_analysis, video_status
from ..stats import metadata_values
from ..views import run_views, views_for
from ..poses import load_analysis_h5
from .. import settings as app_settings
from ..project import (
    NAMING_PRESETS, add_video_folder, add_videos, check_project, init_project, local_videos_dir,
    _store_path, naming_preview, save_subjects, set_models, slug, subjects_table, update_project,
)
from ..sleap_runner import has_predictions, models_for, prediction_paths, run_inference
from ..tracks import load_tracks, quality_mask
from ..video import FrameReader, probe
from .jobs import Job, JobQueue

STATIC = Path(__file__).parent / "static"
RECENT_FILE = app_settings.recent_file()

app = FastAPI(title="Rodent Feeding Behavior")


def _code_fingerprint() -> str:
    """Hash of this package's Python and UI files, to detect a server older than its code."""
    import hashlib

    h = hashlib.sha256()
    pkg = Path(__file__).resolve().parents[1]
    for f in sorted(list(pkg.rglob("*.py")) + list(STATIC.glob("*"))):
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(str(f.relative_to(pkg)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


_STARTED_WITH = _code_fingerprint()


@app.get("/api/version")
def api_version():
    """``stale`` is true when the code on disk changed after this server started (restart it)."""
    now = _code_fingerprint()
    return {"running": _STARTED_WITH, "on_disk": now, "stale": now != _STARTED_WITH}
jobs = JobQueue()
frames = FrameReader()
_state: dict[str, Config | None] = {"cfg": None}
_props: dict[str, dict] = {}
_tracks_cache: OrderedDict[tuple, tuple[pd.DataFrame, dict]] = OrderedDict()
_tracks_lock = threading.Lock()


# --------------------------------------------------------------------------- project state


def C() -> Config:
    """The open project's config (409 if none is open)."""
    cfg = _state["cfg"]
    if cfg is None:
        raise HTTPException(409, "No project is open")
    return cfg


def _recent() -> list[dict]:
    try:
        items = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in items if (Path(r["path"]) / PROJECT_CONFIG_NAME).exists()]


def _remember(cfg: Config) -> None:
    items = [r for r in _recent() if r["path"] != str(cfg.root)]
    items.insert(0, {"path": str(cfg.root), "name": cfg.project_name,
                     "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(json.dumps(items[:12], indent=2), encoding="utf-8")


def open_project(path: str | Path) -> Config:
    cfg = load_project(path)
    _state["cfg"] = cfg
    with _tracks_lock:
        _tracks_cache.clear()
    frames.close()
    _remember(cfg)
    return cfg


try:  # initial project: FEEDING_PROJECT (set by `feeding serve PROJECT`) or the current folder
    open_project(os.environ.get("FEEDING_PROJECT") or Path.cwd())
except (NoProjectError, ValueError):
    pass


# --------------------------------------------------------------------------- helpers


def _video_path(video: str) -> Path:
    cfg = C()
    try:
        parse_video_name(video, cfg.naming.pattern)
    except ValueError:
        raise HTTPException(404, f"unknown video {video}")
    for d in cfg.paths.videos:
        for ext in VIDEO_EXTENSIONS:
            p = d / f"{video}{ext}"
            if p.exists():
                return p
    raise HTTPException(404, f"video file for {video} not found")


def _models(video: str) -> list[Path]:
    try:
        return models_for(C(), video)
    except KeyError:
        return []


def _props_for(video: str) -> dict:
    path = _video_path(video)
    if str(path) not in _props:
        p = probe(path)
        _props[str(path)] = {"width": p.width, "height": p.height, "fps": p.fps, "n_frames": p.n_frames}
    return _props[str(path)]


def _viewer_tracks(video: str) -> tuple[pd.DataFrame, dict] | None:
    """Tracks for display: full preprocessing when the bowl is annotated, poses + quality otherwise."""
    cfg = C()
    h5 = prediction_paths(cfg, video)["h5"]
    if not h5.exists():
        return None
    bowl_p = cfg.paths.bowls / f"{video}.json"
    key = (str(cfg.root), video, h5.stat().st_mtime_ns, bowl_p.stat().st_mtime_ns if bowl_p.exists() else None)
    with _tracks_lock:
        if key in _tracks_cache:
            _tracks_cache.move_to_end(key)
            return _tracks_cache[key]
    if bowl_p.exists():
        df, info = load_tracks(cfg, video, _video_path(video))
    else:
        df = load_analysis_h5(h5, n_frames=_props_for(video)["n_frames"])
        q = cfg.quality
        df["valid"] = quality_mask(df, cfg.tracked_nodes, q.score_window, q.min_score)
        df["in_bowl"] = False
        info = {"fps": _props_for(video)["fps"]}
    with _tracks_lock:
        _tracks_cache[key] = (df, info)
        while len(_tracks_cache) > 6:
            _tracks_cache.popitem(last=False)
    return df, info


def _clean(a: np.ndarray, nd: int = 2) -> list:
    return [None if not math.isfinite(v) else round(float(v), nd) for v in a]


def _bowl_payload(ann: BowlAnnotation | None) -> dict | None:
    if ann is None:
        return None
    try:
        rim = ann.rim().to_dict()
    except ValueError:
        rim = None
    return {**ann.model_dump(mode="json"), "rim": rim}


# --------------------------------------------------------------------------- projects


def _project_payload() -> dict:
    cfg = _state["cfg"]
    base = {"recent": _recent(), "defaults": {
        "pattern": DEFAULT_NAME_PATTERN, "sleap_bin": str(app_settings.find_sleap_bin() or ""),
        "projects_dir": str(app_settings.projects_dir()),
        "model_library": [str(d) for d in app_settings.model_library()],
    }}
    if cfg is None:
        return {"open": False, **base}
    return {
        "open": True, **base,
        "name": cfg.project_name,
        "root": str(cfg.root),
        "config_path": str(cfg.source),
        "analysis_hash": cfg.analysis_hash(),
        "issues": check_project(cfg),
        "nodes": cfg.skeleton.nodes,
        "edges": cfg.skeleton.edges,
        "tracked_nodes": cfg.tracked_nodes,
        "bout_node": cfg.bouts.node,
        "margin_px": cfg.bouts.margin_px,
        "min_score": cfg.quality.min_score,
        "video_dirs": [str(d) for d in cfg.paths.videos],
        "models": {k: [str(p) for p in (v if isinstance(v, list) else [v])] for k, v in cfg.sleap.models.items()},
    }


@app.get("/api/project")
def api_project():
    return _project_payload()


class OpenIn(BaseModel):
    path: str


@app.post("/api/project/open")
def api_open(body: OpenIn):
    try:
        open_project(Path(body.path).expanduser())
    except (NoProjectError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, f"invalid {PROJECT_CONFIG_NAME}: {exc}")
    return _project_payload()


@app.post("/api/project/close")
def api_close():
    _state["cfg"] = None
    frames.close()
    return _project_payload()


class NewProjectIn(BaseModel):
    name: str
    location: str | None = None          # default: <projects folder>/<name>
    model: str | None = None             # optional default SLEAP model folder
    videos_folder: str | None = None     # optional folder of videos, referenced in place
    video_files: list[str] = []          # video files on this machine to copy into <project>/videos
    video_names: list[str] = []          # names of files the browser will upload next (for the naming guess)


@app.post("/api/project/new")
def api_new(body: NewProjectIn):
    if not body.name.strip():
        raise HTTPException(422, "a project name is required")
    root = Path(body.location).expanduser() if body.location else app_settings.projects_dir() / slug(body.name)
    models = {"default": [Path(body.model).expanduser()]} if body.model else None
    try:
        cfg_path, notes = init_project(root, models, Path(body.videos_folder) if body.videos_folder else None,
                                       name=body.name.strip(),
                                       video_names=body.video_names + [Path(f).name for f in body.video_files])
        if body.video_files:
            add_videos(load_project(cfg_path), [Path(f) for f in body.video_files])
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    open_project(cfg_path)
    return {**_project_payload(), "notes": notes}


class DemoIn(BaseModel):
    location: str | None = None  # default: <projects folder>/demo


@app.post("/api/project/demo")
def api_demo(body: DemoIn):
    """Create (or reopen) the synthetic demo project and open it."""
    from ..demo import make_demo

    root = Path(body.location).expanduser() if body.location else app_settings.projects_dir() / "demo"
    if not (root / PROJECT_CONFIG_NAME).exists():
        make_demo(root, log=lambda s: None)
    open_project(root)
    return _project_payload()


# --------------------------------------------------------------------------- project settings


def _settings_payload() -> dict:
    cfg = C()
    project_models = app_settings.find_models([cfg.root / "models"])
    library = app_settings.find_models(app_settings.model_library())
    contexts = sorted({r["context"] for r in video_status(cfg).to_dict("records")} if cfg.paths.videos else set())
    return {
        "name": cfg.project_name,
        "root": str(cfg.root),
        "video_dirs": [{"path": str(d), "exists": d.is_dir(), "local": d == cfg.root / "videos"} for d in cfg.paths.videos],
        "pattern": cfg.naming.pattern,
        "presets": NAMING_PRESETS,
        "chambers": cfg.naming.chambers,
        "models": {k: [str(p) for p in (v if isinstance(v, list) else [v])] for k, v in cfg.sleap.models.items()},
        "available_models": project_models + [m for m in library if m["resolved"] not in {x["resolved"] for x in project_models}],
        "contexts": contexts,
        "sleap_bin": str(cfg.sleap.bin_dir or ""),
        "sleap_found": app_settings.sleap_exe(cfg.sleap.bin_dir, "sleap-track") is not None,
        "sleap_params": cfg.sleap.params(),
        "sleap_param_info": _sleap_param_info(),
        "conditions": cfg.analysis.conditions,
        "skeleton": cfg.skeleton.nodes,
        "subjects": subjects_table(cfg),
    }


def _sleap_param_info() -> list[dict]:
    """Label, help, type, default and choices of each SLEAP inference parameter (for the settings form)."""
    out = []
    for name, (label, help_) in SLEAP_PARAMS.items():
        f = SleapConfig.model_fields[name]
        ann = str(f.annotation)
        choices = list(f.annotation.__args__) if "Literal" in ann else None
        kind = ("choice" if choices else "bool" if f.annotation is bool else "int" if f.annotation is int
                else "float" if f.annotation is float else "list" if name == "extra_args" else "text")
        default = f.get_default(call_default_factory=True)
        out.append({"name": name, "label": label, "help": help_, "kind": kind, "choices": choices, "default": default,
                    "tracking": name not in ("batch_size", "peak_threshold", "device", "tracker", "extra_args")})
    return out


@app.get("/api/project/settings")
def api_settings():
    return _settings_payload()


class SettingsIn(BaseModel):
    name: str | None = None
    pattern: str | None = None
    chambers: dict[str, str] | None = None
    models: dict[str, list[str]] | None = None   # context -> model folder(s); {} = none
    sleap_bin: str | None = None
    sleap_params: dict | None = None             # SLEAP_PARAMS name -> value
    conditions: list[str] | None = None
    video_dirs: list[str] | None = None
    subjects: list[dict] | None = None


@app.put("/api/project/settings")
def api_save_settings(body: SettingsIn):
    cfg = C()
    notes: list[str] = []
    changes = {}
    if body.name is not None:
        changes["name"] = body.name.strip() or None
    if body.pattern is not None:
        changes["naming.pattern"] = body.pattern
    if body.chambers is not None:
        changes["naming.chambers"] = {k: v for k, v in body.chambers.items() if k and v}
    if body.sleap_bin is not None:
        changes["sleap.bin_dir"] = body.sleap_bin.strip() or None
    if body.sleap_params is not None:
        unknown = set(body.sleap_params) - set(SLEAP_PARAMS)
        if unknown:
            raise HTTPException(422, f"unknown SLEAP parameters: {sorted(unknown)}")
        try:
            SleapConfig.model_validate(body.sleap_params)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        changes.update({f"sleap.{k}": v for k, v in body.sleap_params.items()})
    if body.conditions is not None:
        changes["analysis.conditions"] = body.conditions
    if body.video_dirs is not None:
        changes["paths.videos"] = [_store_path(cfg.root, Path(d)) for d in body.video_dirs]
    try:
        if changes:
            cfg = update_project(cfg, changes)
        if body.models is not None:
            models = {k.strip() or "default": [Path(x).expanduser() for x in v if x] for k, v in body.models.items()}
            cfg, notes = set_models(cfg, {k: v for k, v in models.items() if v})
        if body.subjects is not None:
            save_subjects(cfg, body.subjects)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    open_project(cfg.source)
    return {**_settings_payload(), "notes": notes}


@app.get("/api/project/naming-preview")
def api_naming_preview(pattern: str):
    return naming_preview(C(), pattern)


@app.put("/api/project/videos/upload")
async def api_upload_video(request: Request, name: str, overwrite: bool = False):
    """Stream one video file (request body) into <project>/videos."""
    cfg = C()
    fname = Path(name).name
    if Path(fname).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(422, f"{fname}: not a supported video type ({', '.join(VIDEO_EXTENSIONS)})")
    cfg, dest_dir = local_videos_dir(cfg)
    dest = dest_dir / fname
    if dest.exists() and not overwrite:
        raise HTTPException(409, f"{fname} already exists in the project")
    tmp = dest.with_name(f".{fname}.part")
    with open(tmp, "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    frames.close(dest)
    tmp.replace(dest)
    _props.pop(str(dest), None)
    if _state["cfg"] is not None and _state["cfg"].root == cfg.root:
        _state["cfg"] = cfg
    try:
        parse_video_name(dest.stem, cfg.naming.pattern)
        matches = True
    except ValueError:
        matches = False
    return {"file": fname, "path": str(dest), "bytes": dest.stat().st_size, "matches_pattern": matches}


class FolderIn(BaseModel):
    path: str


@app.post("/api/project/videos/folder")
def api_add_video_folder(body: FolderIn):
    try:
        cfg = add_video_folder(C(), Path(body.path))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    open_project(cfg.source)
    return _settings_payload()


class ImportModelIn(BaseModel):
    path: str


@app.post("/api/project/models/import")
def api_import_model(body: ImportModelIn):
    """Copy a SLEAP model folder into <project>/models (so the project is self-contained)."""
    cfg = C()
    src = Path(body.path).expanduser().resolve()
    if not app_settings.is_model_dir(src):
        raise HTTPException(422, f"{src} is not a SLEAP model folder")
    dest = cfg.root / "models" / src.name
    if not dest.exists():
        shutil.copytree(src, dest)
    return {"path": str(dest), **_settings_payload()}


class PathsIn(BaseModel):
    paths: list[str]


@app.post("/api/project/videos/add")
def api_add_video_files(body: PathsIn):
    """Copy video files that are on this machine into <project>/videos."""
    try:
        cfg, added = add_videos(C(), [Path(p).expanduser() for p in body.paths])
    except FileExistsError as exc:
        raise HTTPException(409, str(exc))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    open_project(cfg.source)
    return {"added": [str(p) for p in added], **_settings_payload()}


class PickIn(BaseModel):
    kind: str = "folder"   # "folder" | "videos"
    title: str = "Choose"
    start: str | None = None


@app.post("/api/native/pick")
def api_native_pick(body: PickIn, request: Request):
    """Open the OS folder/file chooser on this machine (only for requests from this machine)."""
    local = request.client is not None and request.client.host in ("127.0.0.1", "::1", "localhost")
    if not local or os.environ.get("FEEDING_NATIVE_DIALOGS", "1") == "0":
        return {"supported": False, "reason": "native dialogs are only available on the machine running the server"}
    from ..native_dialogs import NotSupported, pick

    start = body.start or (str(C().root) if _state["cfg"] else str(Path.home()))
    try:
        paths = pick("videos" if body.kind == "videos" else "folder", body.title, start)
    except NotSupported as exc:
        return {"supported": False, "reason": str(exc)}
    return {"supported": True, "paths": paths}


@app.get("/api/models")
def api_models():
    folders = app_settings.model_library() + ([C().root / "models"] if _state["cfg"] else [])
    return app_settings.find_models(folders)


@app.get("/api/fs")
def api_fs(path: str | None = None):
    """Directory listing for the folder pickers (directories only, annotated)."""
    p = Path(path).expanduser() if path else (C().root.parent if _state["cfg"] else Path.home())
    p = p.resolve()
    if not p.is_dir():
        raise HTTPException(404, f"not a folder: {p}")
    entries = []
    try:
        children = sorted((c for c in p.iterdir() if c.is_dir() and not _hidden(c)), key=lambda c: c.name.lower())
    except PermissionError:
        children = []
    for c in children[:500]:
        try:
            n_videos = sum(1 for f in c.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS)
        except (PermissionError, OSError):
            n_videos = 0
        entries.append({
            "name": c.name, "path": str(c),
            "is_project": (c / PROJECT_CONFIG_NAME).exists(),
            "is_model": (c / "training_config.json").exists(),
            "n_videos": n_videos,
        })
    here_videos = sum(1 for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS) if p.is_dir() else 0
    return {
        "path": str(p), "parent": str(p.parent) if p.parent != p else None, "home": str(Path.home()),
        "is_project": (p / PROJECT_CONFIG_NAME).exists(), "is_model": (p / "training_config.json").exists(),
        "n_videos": here_videos, "entries": entries, "drives": _drives(),
    }


def _hidden(p: Path) -> bool:
    """Hidden folders: dot-folders, and on Windows those marked hidden or system ($Recycle.Bin, ...)."""
    if p.name.startswith((".", "$")) or p.name == "System Volume Information":
        return True
    try:
        return bool(getattr(p.stat(), "st_file_attributes", 0) & 0x6)  # FILE_ATTRIBUTE_HIDDEN | SYSTEM
    except OSError:
        return False


def _drives() -> list[str]:
    """The drive roots on Windows (C:\\, D:\\, ...); empty elsewhere."""
    if os.name != "nt":
        return []
    if hasattr(os, "listdrives"):  # Python 3.12+
        return list(os.listdrives())
    return [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]


# --------------------------------------------------------------------------- videos


@app.get("/api/config")
def api_config():
    return _project_payload() if _state["cfg"] else C()


@app.get("/api/videos")
def api_videos():
    cfg = C()
    st = video_status(cfg)
    out = []
    for r in st.itertuples(index=False):
        job = jobs.active_for(r.video, "infer", str(cfg.root))
        out.append({
            "video": r.video, "session": r.session, "part": None if pd.isna(r.part) else r.part,
            "condition": r.condition, "context": r.context,
            "chamber": None if pd.isna(r.chamber) else r.chamber, "animal": r.animal,
            "group": None if pd.isna(r.group) else r.group,
            "has_predictions": bool(r.has_predictions), "has_bowl": bool(r.has_bowl),
            "job": job.to_dict() if job else None,
        })
    return out


def _params_differ(prov: dict | None, cfg: Config) -> list[str]:
    """SLEAP parameters that changed since these predictions were made (empty when unknown)."""
    made = (prov or {}).get("sleap_params")
    if not made:
        return []
    now = cfg.sleap.params()
    return [k for k in now if k in made and made[k] != now[k]]


@app.get("/api/videos/{video}")
def api_video(video: str):
    cfg = C()
    _video_path(video)
    prov_p = prediction_paths(cfg, video)["provenance"]
    prov = json.loads(prov_p.read_text(encoding="utf-8")) if prov_p.exists() else None
    current = [str(m.resolve()) for m in _models(video)]
    made_with = None
    if prov and prov.get("model"):
        m = prov["model"] if isinstance(prov["model"], list) else [prov["model"]]
        made_with = [str(Path(x["path"]).resolve()) for x in m]
    return {
        "video": video,
        **parse_video_name(video, cfg.naming.pattern).as_dict(cfg.naming.chambers),
        "models": [str(m) for m in _models(video)],
        "predictions_model": made_with,
        "predictions_model_differs": bool(made_with and current and made_with != current),
        "predictions_params_differ": _params_differ(prov, cfg),
        "props": _props_for(video),
        "bowl": _bowl_payload(load_bowl(cfg.paths.bowls, video)),
        "has_predictions": has_predictions(cfg, video),
        "provenance": prov,
    }


@app.get("/api/videos/{video}/frame/{idx}")
def api_frame(video: str, idx: int):
    n = _props_for(video)["n_frames"]
    if not 0 <= idx < n:
        raise HTTPException(404, "frame out of range")
    try:
        data = frames.jpeg(_video_path(video), idx)
    except IndexError as exc:
        raise HTTPException(404, str(exc))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/videos/{video}/poses")
def api_poses(video: str, start: int = 0, count: int = 600):
    t = _viewer_tracks(video)
    if t is None:
        raise HTTPException(404, "no predictions")
    df, _ = t
    count = max(1, min(count, 3000))
    sub = df.iloc[start:start + count]
    return {
        "start": start,
        "count": len(sub),
        "nodes": {n: {"x": _clean(sub[f"{n}_x"].to_numpy()), "y": _clean(sub[f"{n}_y"].to_numpy()),
                      "s": _clean(sub[f"{n}_score"].to_numpy())} for n in C().skeleton.nodes},
        "valid": sub["valid"].astype(int).tolist(),
        "in_bowl": sub["in_bowl"].astype(int).tolist(),
    }


@app.get("/api/videos/{video}/timeline")
def api_timeline(video: str, bins: int = 800):
    t = _viewer_tracks(video)
    if t is None:
        raise HTTPException(404, "no predictions")
    df, info = t
    n = len(df)
    bins = max(1, min(bins, n))
    edges = np.linspace(0, n, bins + 1).astype(int)
    valid = np.add.reduceat(df["valid"].to_numpy(float), edges[:-1]) / np.diff(edges)
    in_bowl = np.add.reduceat(df["in_bowl"].to_numpy(float), edges[:-1]) / np.diff(edges)
    bouts = detect_bouts(df, C().bouts) if "segment" in df else pd.DataFrame()
    return {
        "n_frames": n,
        "fps": info.get("fps"),
        "valid": _clean(valid, 3),
        "in_bowl": _clean(in_bowl, 3),
        "bouts": bouts[["window_start", "contact_start", "contact_end", "window_end", "n_episodes"]].to_dict("records")
        if len(bouts) else [],
        "summary": {
            "valid_frames": int(df["valid"].sum()),
            "in_bowl_frames": int(df["in_bowl"].sum()),
            "n_bouts": len(bouts),
        },
    }


# --------------------------------------------------------------------------- bowl


class BowlIn(BaseModel):
    shape: str = "ellipse"
    center: tuple[float, float]
    top: tuple[float, float] | None = None
    bottom: tuple[float, float] | None = None
    left: tuple[float, float] | None = None
    right: tuple[float, float] | None = None
    polygon: list[tuple[float, float]] | None = None
    frame_idx: int = 0
    note: str | None = None


@app.put("/api/videos/{video}/bowl")
def api_save_bowl(video: str, body: BowlIn):
    cfg = C()
    _video_path(video)
    try:
        ann = BowlAnnotation(video=video, **body.model_dump())
        ann.rim()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    save_bowl(cfg.paths.bowls, ann)
    return _bowl_payload(load_bowl(cfg.paths.bowls, video))


@app.delete("/api/videos/{video}/bowl")
def api_delete_bowl(video: str):
    delete_bowl(C().paths.bowls, video)
    return {"ok": True}


# --------------------------------------------------------------------------- jobs


class InferIn(BaseModel):
    videos: list[str] = []
    all_missing: bool = False
    force: bool = False


def _infer_job(cfg: Config, path: Path):
    def run(job: Job):
        def progress(msg: dict):
            job.progress = {k: msg.get(k) for k in ("n_processed", "n_total", "rate", "eta", "elapsed")}

        return run_inference(cfg, path, on_progress=progress, on_log=job.log,
                             should_stop=lambda: job.cancel_requested)

    return run


@app.post("/api/jobs/infer")
def api_infer(body: InferIn):
    cfg = C()
    st = video_status(cfg)
    if body.all_missing:
        todo = st if body.force else st[~st.has_predictions]
    else:
        todo = st[st.video.isin(body.videos)]
        if not body.force:
            todo = todo[~todo.has_predictions]
    submitted = []
    for r in todo.itertuples(index=False):
        if jobs.active_for(r.video, "infer", str(cfg.root)):
            continue
        job = jobs.submit("infer", f"SLEAP inference: {r.video}", _infer_job(cfg, Path(r.path)), video=r.video,
                          project=str(cfg.root), project_name=cfg.project_name, log_dir=cfg.paths.logs)
        submitted.append(job.id)
    return {"submitted": submitted}


class AnalyzeIn(BaseModel):
    plots: bool = True


@app.post("/api/jobs/analyze")
def api_analyze(body: AnalyzeIn):
    cfg = C()

    def run(job: Job):
        return run_analysis(cfg, make_plots=body.plots, log=job.log)

    job = jobs.submit("analyze", "Analysis run" + ("" if body.plots else " (no figures)"), run,
                      project=str(cfg.root), project_name=cfg.project_name, log_dir=cfg.paths.logs)
    return {"submitted": [job.id]}


@app.get("/api/jobs")
def api_jobs():
    return [j.to_dict() for j in jobs.list()]


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    return job.to_dict(with_log=True)


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: str):
    jobs.cancel(jid)
    return {"ok": True}


@app.post("/api/jobs/clear")
def api_clear():
    jobs.clear_finished()
    return {"ok": True}


# --------------------------------------------------------------------------- views


def _local(request: Request) -> bool:
    return request.client is not None and request.client.host in ("127.0.0.1", "::1", "localhost", "testclient")


@app.get("/api/views")
def api_views():
    """The project's comparison views, the standard view, and the metadata values to build groups from."""
    cfg = C()
    meta = video_status(cfg)
    std = [v for v in views_for(cfg.model_copy(update={"views": []}), meta) if v.slug == "standard"]
    return {
        "views": [v.model_dump(mode="json") for v in cfg.views],
        "standard": std[0].model_dump(mode="json") if std else None,
        "standard_enabled": cfg.analysis.standard_view,
        "fields": metadata_values(meta),
        "analysis": cfg.analysis.model_dump(mode="json"),
    }


class ViewsIn(BaseModel):
    views: list[dict]
    standard_enabled: bool | None = None


@app.put("/api/views")
def api_views_put(body: ViewsIn):
    from ..project import set_views

    cfg = C()
    try:
        cfg = set_views(cfg, body.views)
        if body.standard_enabled is not None and body.standard_enabled != cfg.analysis.standard_view:
            cfg = update_project(cfg, {"analysis.standard_view": body.standard_enabled})
    except Exception as exc:
        raise HTTPException(422, str(exc))
    _state["cfg"] = cfg
    return api_views()


# --------------------------------------------------------------------------- results


def _run_dir(run: str) -> Path:
    root = C().paths.results.resolve()
    d = (root / run).resolve()
    if d.parent != root or not (d / "manifest.json").exists():
        raise HTTPException(404, f"no analysis run {run!r}")
    return d


def _rel_files(d: Path, base: Path, pattern: str) -> list[str]:
    from ..naming import natural_key

    return sorted((p.relative_to(base).as_posix() for p in d.glob(pattern) if p.is_file()), key=natural_key) if d.exists() else []


def _views_index(d: Path) -> list[dict]:
    p = d / "views" / "index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


@app.get("/api/results")
def api_results():
    root = C().paths.results
    runs = []
    for d in sorted(root.glob("*/"), reverse=True) if root.exists() else []:
        m = d / "manifest.json"
        if not m.exists():
            continue
        man = json.loads(m.read_text(encoding="utf-8"))
        runs.append({
            "name": d.name, "created_at": man.get("created_at"), "n_videos": man.get("n_videos_analysed"),
            "n_bouts": man.get("n_bouts"), "n_skipped": len(man.get("skipped", {})), "git": man.get("git"),
            "views": [v["name"] for v in _views_index(d)],
        })
    return runs


@app.get("/api/results/{run}")
def api_result(run: str):
    d = _run_dir(run)
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    ex = d / "exploratory" / "summary.json"
    sessions = d / "sessions.csv"
    return {
        "name": run, "path": str(d), "created_at": man.get("created_at"), "elapsed_s": man.get("elapsed_s"),
        "n_videos": man.get("n_videos_analysed"), "n_bouts": man.get("n_bouts"), "skipped": man.get("skipped", {}),
        "git": man.get("git"), "analysis_hash": man.get("analysis_hash"),
        "analysis": man.get("config", {}).get("analysis"), "packages": man.get("packages"),
        "views": _views_index(d),
        "exploratory": {"figures": _rel_files(d / "exploratory", d, "*.png"),
                        "per_animal": _rel_files(d / "exploratory" / "per_animal", d, "*.png"),
                        "summary": json.loads(ex.read_text(encoding="utf-8")) if ex.exists() else {}},
        "per_animal": {k: _rel_files(d / "figures" / k, d, "*.png") for k in ("heatmaps", "tornado_distance", "tornado_speed")},
        "files": sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file() and p.suffix in (".csv", ".xlsx", ".json", ".yaml", ".npz")),
        "notes": man.get("notes", []),
        "sessions": _records(sessions),
    }


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    return json.loads(df.to_json(orient="records"))


@app.get("/api/results/{run}/views/{slug}")
def api_result_view(run: str, slug: str):
    d = _run_dir(run)
    v = d / "views" / slug
    if not (v / "view.json").exists():
        raise HTTPException(404, f"view {slug!r} has not been run on {run}")
    return {
        **json.loads((v / "view.json").read_text(encoding="utf-8")),
        "stats_bouts": _records(v / "stats_bouts.csv"), "stats_sessions": _records(v / "stats_sessions.csv"),
        "omnibus_bouts": _records(v / "omnibus_bouts.csv"), "omnibus_sessions": _records(v / "omnibus_sessions.csv"),
        "figures": _rel_files(v / "figures", d, "*.png") + _rel_files(v / "figures", d, "*/*.png"),
        "files": _rel_files(v, d, "*.*"),
    }


class RunViewsIn(BaseModel):
    slugs: list[str] | None = None  # project views (or "standard") to run; default all
    plots: bool = True


@app.post("/api/results/{run}/views")
def api_run_views(run: str, body: RunViewsIn):
    cfg = C()
    d = _run_dir(run)
    from ..views import load_run_tables

    sessions = load_run_tables(d)[1]
    views = views_for(cfg, sessions)
    if body.slugs is not None:
        views = [v for v in views if v.slug in body.slugs]
        if not views:
            raise HTTPException(422, "none of those views exist in the project")
    names = ", ".join(v.name for v in views)

    def job_fn(job: Job):
        out = run_views(d, cfg, views, make_plots=body.plots, log=job.log)
        bad = [v for v in out if v.get("error")]
        if bad:
            raise RuntimeError("; ".join(f"{v['name']}: {v['error']}" for v in bad))
        return d

    job = jobs.submit("views", f"Compare: {names}"[:120] + f" (on {run})", job_fn,
                      project=str(cfg.root), project_name=cfg.project_name, log_dir=cfg.paths.logs)
    return {"submitted": [job.id]}


@app.get("/api/results/{run}/export.zip")
def api_export(run: str, sub: str | None = None):
    """The whole run (or one sub-folder, e.g. ``views/standard``) as a zip, streamed."""
    import tempfile
    import zipfile

    d = _run_dir(run)
    base = (d / sub).resolve() if sub else d
    if base != d and d not in base.parents or not base.is_dir():
        raise HTTPException(404)
    tmp = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(base.rglob("*")):
            if p.is_file():
                z.write(p, Path(run) / p.relative_to(d))
    size = tmp.tell()
    tmp.seek(0)
    name = run + ("_" + sub.replace("/", "_") if sub else "") + ".zip"
    return StreamingResponse(iter(lambda: tmp.read(1 << 20), b""), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Content-Length": str(size)})


@app.post("/api/results/{run}/reveal")
def api_reveal(run: str, request: Request, sub: str | None = None):
    """Show the run folder in Finder / the file manager (only on the machine running the server)."""
    d = _run_dir(run)
    target = (d / sub).resolve() if sub else d
    if target != d and d not in target.parents:
        raise HTTPException(404)
    if not _local(request):
        return {"ok": False, "path": str(target), "reason": "only available on the machine running the server"}
    from ..native_dialogs import NotSupported, reveal

    try:
        reveal(target)
    except NotSupported as exc:
        return {"ok": False, "path": str(target), "reason": str(exc)}
    return {"ok": True, "path": str(target)}


@app.get("/results/{run}/{path:path}")
def results_file(run: str, path: str):
    root = C().paths.results.resolve()
    p = (root / run / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# --------------------------------------------------------------------------- help (the user manual)


@app.get("/api/docs")
def api_docs():
    from .. import docs

    return [{**c, "toc": docs.chapter(c["slug"])["toc"]} for c in docs.chapters()]


@app.get("/api/docs/search")
def api_docs_search(q: str):
    from .. import docs

    return docs.search(q)


@app.get("/api/docs/{slug}")
def api_doc(slug: str):
    from .. import docs

    d = docs.chapter(slug)
    if d is None:
        raise HTTPException(404, f"no chapter {slug!r}")
    return {k: d[k] for k in ("slug", "title", "html", "toc")}


@app.get("/docs/manual/{path:path}")
def doc_file(path: str):
    from .. import docs

    root = docs.docs_dir().resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=3600"})


@app.get("/docs/{name}.docx")
def doc_word(name: str):
    from .. import docs

    p = docs.docs_dir().parent / f"{Path(name).name}.docx"
    if not p.is_file():
        raise HTTPException(404, "the Word manual is not built (python scripts/build_manual.py)")
    return FileResponse(p, filename=p.name)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
