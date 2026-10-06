"""Project folders: creation, editing and checks.

Project layout::

    <project>/
      feeding.yaml              configuration (only this file is required)
      videos/                   per-animal videos (default; paths.videos may list other folders too)
      models/                   SLEAP model folders (optional; sleap.models may point anywhere)
      metadata/subjects.csv     animal -> group (needed for group statistics)
      annotations/bowls/        bowl annotations written by the UI
      data/                     generated: predictions, derived tracks, results, logs (git-ignored)

A project can start barebones -- a name and at least one video -- and grow:
videos and models can be added or changed at any time (UI settings or CLI).
"""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from .config import (
    DEFAULT_NAME_PATTERN, PROJECT_CONFIG_NAME, TEMPLATE, Config, load_project, skeleton_from_model,
)
from .settings import sleap_exe
from .naming import VIDEO_EXTENSIONS, list_videos, load_subjects, natural_key, parse_video_name, video_files

GENERIC_PATTERN = r"^(?P<animal>.+)$"  # whole file name = animal ID
NAMING_PRESETS = {
    "{session}[_{part}]_{condition}_{context}_{animal}": DEFAULT_NAME_PATTERN,
    "whole file name is the animal": GENERIC_PATTERN,
    "{animal}_{condition}": r"^(?P<animal>[^_]+)_(?P<condition>[^_]+)$",
    "{animal}_{condition}_{context}": r"^(?P<animal>[^_]+)_(?P<condition>[^_]+)_(?P<context>[^_]+)$",
}
SINGLE_ANIMAL_HEADS = ({"single_instance"}, {"centroid", "centered_instance"})


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-") or "project"


def guess_pattern(names: list[str]) -> str:
    """The default naming if every name fits it, else 'whole file name = animal'."""
    stems = [Path(n).stem if Path(n).suffix.lower() in VIDEO_EXTENSIONS else n for n in names]
    if stems and all(re.match(DEFAULT_NAME_PATTERN, s) for s in stems):
        return DEFAULT_NAME_PATTERN
    return GENERIC_PATTERN


def parse_model_args(args: list[str]) -> dict[str, list[Path]]:
    """``PATH`` -> default model; ``CONTEXT=PATH`` -> model for that context.
    Repeating a key gives a list (e.g. top-down centroid + centered-instance)."""
    out: dict[str, list[Path]] = {}
    for a in args:
        key, sep, rest = a.partition("=")
        # Model folder names often contain "=" (e.g. "...single_instance.n=352").
        if sep and re.fullmatch(r"[A-Za-z0-9_-]+", key) and not Path(a).expanduser().exists():
            path = rest
        else:
            key, path = "default", a
        p = Path(path).expanduser().resolve()
        if p.name == "training_config.json":
            p = p.parent
        out.setdefault(key or "default", []).append(p)
    return out


def _store_path(root: Path, p: Path) -> str:
    """Path as written to feeding.yaml: relative if inside the project, else absolute."""
    p = Path(p).expanduser().resolve()
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _skeleton_settings(nodes: list[str], current: dict | None = None) -> tuple[dict, list[str]]:
    """bouts.node / drop_nodes / speed_nodes that are valid for ``nodes``."""
    current = current or {}
    notes = []
    bout_node = current.get("bout_node")
    if bout_node not in nodes:
        new = "Snout" if "Snout" in nodes else (nodes[0] if nodes else "Snout")
        if bout_node and nodes:
            notes.append(f"bouts.node {bout_node!r} is not in the new skeleton; using {new!r}")
        elif nodes and new != "Snout":
            notes.append(f"bouts.node set to {new!r} (no 'Snout' in the skeleton); change it if needed")
        bout_node = new
    drop = [n for n in current.get("drop_nodes", ["TipTail"]) if n in nodes]
    speed = [n for n in (current.get("speed_nodes") or nodes) if n in nodes and n not in drop] or \
        [n for n in nodes if n not in drop]
    return {"bout_node": bout_node, "drop_nodes": drop, "speed_nodes": speed}, notes


def _models_skeleton(models: dict[str, list[Path]], sleap_bin: Path) -> tuple[dict, list[str]]:
    """Skeleton shared by the given models (read from their training_config.json)."""
    notes = []
    infos = {}
    for key, dirs in models.items():
        for d in dirs:
            if not (d / "training_config.json").exists():
                raise FileNotFoundError(f"model {key!r}: {d} is not a SLEAP model folder (no training_config.json)")
        infos[key] = [skeleton_from_model(d) for d in dirs]
        heads = set().union(*(set(i["heads"]) for i in infos[key]))
        if not any(heads == h for h in SINGLE_ANIMAL_HEADS):
            notes.append(f"model {key!r} has heads {sorted(heads)}; the pipeline analyses one animal per video")
    if not infos:
        return {"nodes": [], "edges": []}, notes
    sks = [v[-1] for v in infos.values()]
    for sk in sks[1:]:
        if sk["nodes"] != sks[0]["nodes"]:
            raise ValueError(f"models have different skeletons: {sks[0]['nodes']} vs {sk['nodes']}")
    return {"nodes": sks[0]["nodes"], "edges": sks[0]["edges"]}, notes


def init_project(
    root: Path,
    models: dict[str, list[Path]] | None = None,
    videos: Path | list[Path] | None = None,
    sleap_bin: Path | None = None,
    pattern: str | None = None,
    chambers: dict[str, str] | None = None,
    name: str | None = None,
    conditions: list[str] | None = None,
    video_names: list[str] | None = None,
    force: bool = False,
) -> tuple[Path, list[str]]:
    """Create a project folder; returns (feeding.yaml path, notes for the user).

    Everything but ``root`` is optional: videos default to ``<root>/videos``,
    models can be assigned later, and the naming pattern is guessed from the
    video names (``video_names`` lets a caller that uploads files afterwards
    supply them up front).
    """
    root = Path(root).expanduser().resolve()
    sleap_bin = Path(sleap_bin).expanduser() if sleap_bin else None
    cfg_path = root / PROJECT_CONFIG_NAME
    if cfg_path.exists() and not force:
        raise FileExistsError(f"{cfg_path} already exists (use --force to overwrite)")
    models = models or {}
    notes: list[str] = []

    skeleton, model_notes = _models_skeleton(models, sleap_bin)
    notes += model_notes
    if not models:
        notes.append("no SLEAP model assigned yet; add one in Settings (or put a model folder in models/)")

    folders = [videos] if isinstance(videos, (str, Path)) else list(videos or [])
    folders = [Path(f).expanduser().resolve() for f in folders]
    for f in folders:
        if not f.is_dir():
            raise FileNotFoundError(f"videos folder not found: {f}")
    names = list(video_names or []) + [p.name for p in video_files(folders + [root / "videos"])]
    pattern = pattern or guess_pattern(names)
    found = [n for n in names if _matches(n, pattern)]
    if names and not found:
        raise ValueError(f"no video name matches the naming pattern {pattern!r}; examples: {names[:5]}")
    vinfo = [parse_video_name(n, pattern) for n in found]
    if models and "default" not in models:
        missing = sorted({i.context for i in vinfo} - set(models))
        if missing:
            raise ValueError(f"no model for contexts {missing}; add CONTEXT=PATH models or a default model")

    sk_settings, sk_notes = _skeleton_settings(skeleton["nodes"])
    notes += sk_notes
    found_conds = sorted({i.condition for i in vinfo} - {"NA"}, key=natural_key)
    conds = conditions or []  # empty = every condition, by session order
    j = json.dumps
    root.mkdir(parents=True, exist_ok=True)
    video_dirs = ["videos"] + [_store_path(root, f) for f in folders if f != root / "videos"]
    text = TEMPLATE.read_text().format(
        name=j(name or root.name), videos=j(video_dirs), pattern=j(pattern), chambers=j(chambers or {}),
        bin_dir=j(str(sleap_bin)) if sleap_bin else "null",
        models=j({k: _store_path(root, v[0]) if len(v) == 1 else [_store_path(root, x) for x in v]
                  for k, v in models.items()}),
        nodes=j(skeleton["nodes"]), edges=j(skeleton["edges"]), drop_nodes=j(sk_settings["drop_nodes"]),
        bout_node=j(sk_settings["bout_node"]), speed_nodes=j(sk_settings["speed_nodes"]), conditions=j(conds),
    )
    for sub in ("videos", "models", "annotations/bowls", "metadata", "data"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "annotations" / "bowls" / ".gitkeep").touch()
    cfg_path.write_text(text)
    subjects = root / "metadata" / "subjects.csv"
    if not subjects.exists() or force:
        animals = sorted({i.animal for i in vinfo}, key=natural_key)
        pd.DataFrame({"animal": animals, "group": [""] * len(animals)}).to_csv(subjects, index=False)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(".DS_Store\ndata/\n")
    load_project(cfg_path)  # validate what we wrote
    return cfg_path, notes


def _matches(name: str, pattern: str) -> bool:
    try:
        parse_video_name(name, pattern)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- editing


def update_project(cfg: Config, changes: dict[str, Any]) -> Config:
    """Set dotted keys (``"naming.pattern"``) in feeding.yaml, keeping comments.

    A value of ``None`` removes the key. The result is validated; on error the
    file is restored and the error re-raised. Returns the reloaded project.
    """
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 120
    path = cfg.source
    old_text = path.read_text()
    data = y.load(old_text) or {}
    for dotted, value in changes.items():
        keys = dotted.split(".")
        node = data
        for k in keys[:-1]:
            if node.get(k) is None:
                node[k] = {}
            node = node[k]
        if value is None:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    buf = io.StringIO()
    y.dump(data, buf)
    path.write_text(buf.getvalue())
    try:
        return load_project(path)
    except Exception:
        path.write_text(old_text)
        raise


def set_models(cfg: Config, models: dict[str, list[Path]]) -> tuple[Config, list[str]]:
    """Assign SLEAP models (context -> folders); refreshes the skeleton and node settings.

    Existing predictions are kept; their provenance records the model that made them.
    """
    skeleton, notes = _models_skeleton(models, cfg.sleap.bin_dir)
    root = cfg.root
    changes: dict[str, Any] = {
        "sleap.models": {k: _store_path(root, v[0]) if len(v) == 1 else [_store_path(root, x) for x in v]
                         for k, v in models.items()},
    }
    if skeleton["nodes"]:
        if cfg.skeleton.nodes and skeleton["nodes"] != cfg.skeleton.nodes:
            notes.append(f"skeleton changed from {cfg.skeleton.nodes} to {skeleton['nodes']}; "
                         "re-run SLEAP so predictions match")
        current = {"bout_node": cfg.bouts.node, "drop_nodes": cfg.quality.drop_nodes,
                   "speed_nodes": cfg.features.speed_nodes}
        sk_settings, sk_notes = _skeleton_settings(skeleton["nodes"], current)
        notes += sk_notes
        edges = skeleton["edges"] or [e for e in cfg.skeleton.edges if set(e) <= set(skeleton["nodes"])]
        changes.update({
            "skeleton.nodes": skeleton["nodes"], "skeleton.edges": [list(e) for e in edges],
            "bouts.node": sk_settings["bout_node"], "quality.drop_nodes": sk_settings["drop_nodes"],
            "features.speed_nodes": sk_settings["speed_nodes"],
        })
    return update_project(cfg, changes), notes


def add_video_folder(cfg: Config, folder: Path) -> Config:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"not a folder: {folder}")
    if folder in cfg.paths.videos:
        return cfg
    stored = [_store_path(cfg.root, p) for p in cfg.paths.videos] + [_store_path(cfg.root, folder)]
    return update_project(cfg, {"paths.videos": stored})


def local_videos_dir(cfg: Config) -> tuple[Config, Path]:
    """``<project>/videos`` (created and added to paths.videos if needed) -- where uploads go."""
    d = cfg.root / "videos"
    d.mkdir(parents=True, exist_ok=True)
    return add_video_folder(cfg, d), d


def add_videos(cfg: Config, files: list[Path], move: bool = False) -> tuple[Config, list[Path]]:
    """Copy (or move) video files into ``<project>/videos``."""
    cfg, dest = local_videos_dir(cfg)
    out = []
    for f in files:
        f = Path(f)
        if f.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"not a video file: {f.name}")
        target = dest / f.name
        if target.exists():
            raise FileExistsError(f"{target.name} already exists in {dest}")
        (shutil.move if move else shutil.copy2)(f, target)
        out.append(target)
    return cfg, out


def naming_preview(cfg: Config, pattern: str, limit: int = 40) -> dict:
    files = video_files(cfg.paths.videos)
    rows, n_ok = [], 0
    for p in files:
        try:
            info = parse_video_name(p.stem, pattern).as_dict()
            n_ok += 1
            row = {"file": p.name, "ok": True, **{k: info[k] for k in ("animal", "condition", "context", "session", "part")}}
        except ValueError:
            row = {"file": p.name, "ok": False}
        except re.error as exc:
            return {"error": str(exc), "rows": [], "matched": 0, "total": len(files)}
        if len(rows) < limit:
            rows.append(row)
    return {"rows": rows, "matched": n_ok, "total": len(files)}


# --------------------------------------------------------------------------- checks


def check_project(cfg: Config) -> list[dict]:
    """Problems that would stop or degrade the pipeline: [{level, message}]."""
    issues: list[dict] = []

    def add(level, msg):
        issues.append({"level": level, "message": msg})

    p = cfg.paths
    for d in p.videos:
        if not d.is_dir():
            add("error", f"videos folder not found: {d}")
    files = video_files(p.videos)
    videos = list_videos(p.videos, cfg.naming.pattern)
    if not files:
        add("warning", "no videos yet; add some in Settings or put them in the project's videos/ folder")
    elif len(videos) < len(files):
        add("warning", f"{len(files) - len(videos)} of {len(files)} videos don't match the naming pattern "
                       "and are ignored (see Settings → Naming)")
    if not cfg.sleap.models:
        add("warning", "no SLEAP model assigned; set one in Settings to run inference")
    for d in cfg.sleap.all_model_dirs():
        if not (d / "training_config.json").exists():
            add("error", f"SLEAP model folder not found: {d}")
            continue
        try:
            nodes = skeleton_from_model(d)["nodes"]
        except (KeyError, ValueError):
            add("warning", f"could not read the skeleton of {d}")
            continue
        if cfg.skeleton.nodes and nodes != cfg.skeleton.nodes:
            add("error", f"model {d.name} has nodes {nodes}, but skeleton.nodes is {cfg.skeleton.nodes}")
    infos = [parse_video_name(v.stem, cfg.naming.pattern) for v in videos]
    if cfg.sleap.models and "default" not in cfg.sleap.models:
        unmapped = sorted({i.context for i in infos} - set(cfg.sleap.models))
        if unmapped:
            add("error", f"no SLEAP model for contexts {unmapped}")
    if cfg.sleap.models and sleap_exe(cfg.sleap.bin_dir, "sleap-track") is None:
        where = f"in {cfg.sleap.bin_dir}" if cfg.sleap.bin_dir else "(no SLEAP environment found)"
        add("warning", f"sleap-track not found {where}; set the SLEAP environment in Settings to run inference "
                       "(importing predictions still works)")
    if not p.subjects.exists():
        if infos:
            add("warning", f"no subjects table ({p.subjects.name}); group statistics need it")
    else:
        try:
            subj = load_subjects(p.subjects)
            animals = {i.animal for i in infos}
            missing = sorted(animals - set(subj["animal"]), key=natural_key)
            if missing:
                add("warning", f"{len(missing)} animals missing from {p.subjects.name}: {', '.join(missing[:8])}")
            blank_mask = subj["group"].isna() | (subj["group"].astype(str).str.strip() == "")
            blank = sorted(set(subj.loc[blank_mask, "animal"]) & animals, key=natural_key)
            if blank:
                add("warning", f"{len(blank)} animals have no group in {p.subjects.name}: {', '.join(blank[:8])}")
        except ValueError as exc:
            add("error", str(exc))
    if cfg.embedding.checkpoint is not None and not cfg.embedding.checkpoint.exists():
        add("warning", f"embedding.checkpoint not found: {cfg.embedding.checkpoint}")
    return issues


def save_subjects(cfg: Config, rows: list[dict]) -> None:
    df = pd.DataFrame(rows, columns=["animal", "group"]).fillna("")
    df["animal"] = df["animal"].astype(str).str.strip()
    df = df[df["animal"] != ""].drop_duplicates("animal", keep="last")
    df = df.sort_values("animal", key=lambda s: s.map(natural_key))
    cfg.paths.subjects.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cfg.paths.subjects, index=False)


def subjects_table(cfg: Config) -> list[dict]:
    """Every animal seen in the videos plus any already in subjects.csv."""
    known = load_subjects(cfg.paths.subjects) if cfg.paths.subjects.exists() else pd.DataFrame(columns=["animal", "group"])
    groups = dict(zip(known["animal"], known["group"].fillna("")))
    animals = {parse_video_name(v.stem, cfg.naming.pattern).animal for v in list_videos(cfg.paths.videos, cfg.naming.pattern)}
    return [{"animal": a, "group": groups.get(a, ""), "has_videos": a in animals}
            for a in sorted(animals | set(groups), key=natural_key)]


def set_views(cfg: Config, views: list[dict]) -> Config:
    """Replace the project's comparison views (validated first); ``where`` maps are
    written in flow style so each group stays on one line."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    from .config import View

    parsed = [View.model_validate(v) for v in views]
    seq = CommentedSeq()
    for v in parsed:
        d = v.model_dump(mode="json", exclude_defaults=True)
        m = CommentedMap()
        m["name"] = d["name"]
        if d.get("description"):
            m["description"] = d["description"]
        groups = CommentedSeq()
        for g in d["groups"]:
            gm = CommentedMap(label=g["label"])
            where = CommentedMap()
            for k, vals in g.get("where", {}).items():
                vs = CommentedSeq(vals)
                vs.fa.set_flow_style()
                where[k] = vs
            where.fa.set_flow_style()
            gm["where"] = where
            gm.fa.set_flow_style()
            groups.append(gm)
        m["groups"] = groups
        if d.get("pairs"):
            pairs = CommentedSeq()
            for a, b in d["pairs"]:
                p = CommentedSeq([a, b])
                p.fa.set_flow_style()
                pairs.append(p)
            m["pairs"] = pairs
        if d.get("paired", "auto") != "auto":
            m["paired"] = d["paired"]
        seq.append(m)
    return update_project(cfg, {"views": seq if parsed else None})
