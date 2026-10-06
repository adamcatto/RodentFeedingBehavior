"""Projects and their typed configuration.

A *project* is a folder containing ``feeding.yaml`` (see ``docs/`` and
``feeding/templates/feeding.yaml``). Relative paths in it resolve against the
project folder, and everything has a conventional default: a project can start
as a folder with an (even empty) ``feeding.yaml`` and some videos in ``videos/``;
SLEAP models can be assigned later, or a single model placed in ``models/`` is
used automatically.

The project is found, in order: an explicit path (``--project`` / ``load_project``),
the ``FEEDING_PROJECT`` environment variable, then the current directory and its
parents (like git).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]  # this repository
PROJECT_CONFIG_NAME = "feeding.yaml"
TEMPLATE = PACKAGE_ROOT / "templates" / PROJECT_CONFIG_NAME
# {session}[_{part}]_{condition}_{context}_{animal}, e.g. 3_Pre_A_m12 or 7_2_Post_B_m4.
DEFAULT_NAME_PATTERN = (
    r"^(?P<session>\d+)(?:_(?P<part>\d+))?_(?P<condition>[A-Za-z]+)_(?P<context>[A-Za-z0-9]+)_(?P<animal>[A-Za-z0-9]+)$"
)
# Preferred whole-body keypoints for locomotion, in order (else the first speed node).
BODY_NODES = ("MidBack", "Centroid", "Center", "Centre", "Body", "Thorax", "Spine", "BodyCenter", "Back")


class NoProjectError(FileNotFoundError):
    pass


class Paths(BaseModel):
    videos: list[Path] = Field(default_factory=lambda: [Path("videos")])  # one or more folders
    predictions: Path = Path("data/predictions")
    bowls: Path = Path("annotations/bowls")
    subjects: Path = Path("metadata/subjects.csv")
    derived: Path = Path("data/derived")
    results: Path = Path("data/results")
    logs: Path = Path("data/logs")

    @field_validator("videos", mode="before")
    @classmethod
    def _as_list(cls, v):
        return [v] if isinstance(v, (str, Path)) else v


class NamingConfig(BaseModel):
    """How video file names map to metadata.

    ``pattern`` is a regex matched against each video's file stem. The named
    group ``animal`` is required; ``session``, ``part``, ``condition`` and
    ``context`` are optional (missing ones become "NA"). Videos whose names
    do not match are ignored.
    """

    pattern: str = DEFAULT_NAME_PATTERN
    chambers: dict[str, str] = Field(default_factory=dict)  # optional display label per context

    @field_validator("pattern")
    @classmethod
    def _has_animal(cls, v: str) -> str:
        if re.compile(v).groupindex.get("animal") is None:
            raise ValueError("naming.pattern must contain a named group (?P<animal>...)")
        return v


class SleapConfig(BaseModel):
    """How SLEAP inference is run (``sleap-track`` in SLEAP's own environment).

    ``models`` maps a video context (or ``default``) to one SLEAP model
    directory, or a list of them (e.g. a top-down centroid + centered-instance pair).
    ``bin_dir`` is the ``bin/`` folder of that environment; left empty, it is
    found automatically (see :func:`feeding.settings.find_sleap_bin`).

    The remaining fields are ``sleap-track`` options (see :data:`SLEAP_PARAMS`);
    ``extra_args`` are passed verbatim after them and win over them.
    """

    bin_dir: Path | None = None
    models: dict[str, Path | list[Path]] = Field(default_factory=dict)
    batch_size: int = Field(4, ge=1)
    peak_threshold: float = Field(0.2, ge=0, le=1)
    device: str = "auto"  # auto | cpu | gpu (first GPU) | a GPU index, e.g. "1"
    tracker: Literal["none", "simple", "flow"] = "none"
    target_instance_count: int = Field(0, ge=0)
    pre_cull_to_target: bool = False
    post_connect_single_breaks: bool = False
    clean_instance_count: int = Field(0, ge=0)
    similarity: Literal["instance", "centroid", "iou"] = "instance"
    match: Literal["greedy", "hungarian"] = "greedy"
    track_window: int = Field(5, ge=1)
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("device", mode="before")
    @classmethod
    def _device(cls, v):
        v = str(v).strip().lower()
        if v not in ("auto", "cpu", "gpu") and not v.isdigit():
            raise ValueError("sleap.device must be auto, cpu, gpu or a GPU index (0, 1, ...)")
        return v

    def track_args(self) -> list[str]:
        """``sleap-track`` options for these settings (minus models, input and output)."""
        args = {"--batch_size": str(self.batch_size), "--peak_threshold": f"{self.peak_threshold:g}",
                "--tracking.tracker": self.tracker}
        if self.device == "cpu":
            args["--cpu"] = None
        elif self.device == "gpu":
            args["--first-gpu"] = None
        elif self.device.isdigit():
            args["--gpu"] = self.device
        if self.tracker != "none":
            args.update({
                "--tracking.target_instance_count": str(self.target_instance_count),
                "--tracking.pre_cull_to_target": str(int(self.pre_cull_to_target)),
                "--tracking.post_connect_single_breaks": str(int(self.post_connect_single_breaks)),
                "--tracking.clean_instance_count": str(self.clean_instance_count),
                "--tracking.similarity": self.similarity,
                "--tracking.match": self.match,
                "--tracking.track_window": str(self.track_window),
            })
        overridden = {a.split("=", 1)[0] for a in self.extra_args if a.startswith("-")}
        if overridden & {"--cpu", "--first-gpu", "--last-gpu", "--gpu"}:
            overridden |= {"--cpu", "--first-gpu", "--gpu"}
        out = []
        for flag, value in args.items():
            if flag not in overridden:
                out += [flag] if value is None else [flag, value]
        return out + list(self.extra_args)

    def params(self) -> dict:
        """The inference parameters (everything but the environment and the models)."""
        return self.model_dump(mode="json", exclude={"bin_dir", "models"})

    def model_dirs(self, context: str) -> list[Path]:
        m = self.models.get(context, self.models.get("default"))
        if m is None:
            raise KeyError(f"no SLEAP model assigned for context {context!r} (set one in the project settings)")
        return list(m) if isinstance(m, list) else [m]

    def all_model_dirs(self) -> list[Path]:
        return [p for v in self.models.values() for p in (v if isinstance(v, list) else [v])]


# Inference parameters as shown in the UI and the CLI: name -> (label, help).
SLEAP_PARAMS = {
    "batch_size": ("Batch size", "Frames predicted at a time. Larger is faster but needs more (GPU) memory."),
    "peak_threshold": ("Peak threshold", "Minimum confidence-map value for a keypoint to be detected (0-1). "
                       "Lower finds more keypoints, including more wrong ones."),
    "device": ("Device", "auto (SLEAP picks), cpu, gpu (the first GPU) or a GPU index (0, 1, ...)."),
    "tracker": ("Tracker", "Links instances across frames. 'none' suits one animal per video; "
                "'simple' or 'flow' (optical flow) for several animals."),
    "target_instance_count": ("Target instance count", "Animals per frame the tracker keeps (0 = no limit)."),
    "pre_cull_to_target": ("Cull before tracking", "Drop extra instances per frame before tracking (needs a target count)."),
    "post_connect_single_breaks": ("Connect single breaks", "Join a lost and a new track when exactly one of each "
                                   "occurs in a frame (needs a target count)."),
    "clean_instance_count": ("Clean instance count", "Instances to keep per frame after tracking (0 = off)."),
    "similarity": ("Similarity", "How candidates are compared: instance (keypoints), centroid or iou (boxes)."),
    "match": ("Matching", "greedy or hungarian assignment of instances to tracks."),
    "track_window": ("Track window", "Frames to look back for a match."),
    "extra_args": ("Extra arguments", "Passed verbatim to sleap-track, after (and overriding) the options above."),
}


class SplitConfig(BaseModel):
    raw_videos: Path | None = None
    output: Path = Path("data/split_videos")
    crf: int = 18
    gop: int = 30


class SkeletonConfig(BaseModel):
    nodes: list[str] = Field(default_factory=list)  # empty until a model or predictions exist
    edges: list[tuple[str, str]] = Field(default_factory=list)


class QualityConfig(BaseModel):
    drop_nodes: list[str] = Field(default_factory=list)
    score_window: int = 300
    min_score: float = 0.5
    min_segment_frames: int = 90
    max_interp_gap: int = 0


class BoutConfig(BaseModel):
    node: str = "Snout"
    margin_px: float = 0.0
    min_contact_frames: int = 15
    merge_gap_frames: int = 90
    flank_frames: int = 90


class FeatureConfig(BaseModel):
    speed_nodes: list[str] | None = None  # default: all tracked nodes
    body_node: str | None = None  # whole-session locomotion; default a body-centre node (BODY_NODES) if tracked, else the first speed node
    moving_speed: float = 1.0  # px/frame (5-frame smoothed) above which the animal counts as moving


class EmbeddingConfig(BaseModel):
    """LSTM-VAE behaviour embedding (``feeding embed``; optional ``embed`` extra)."""

    checkpoint: Path | None = None  # a trained model to use; empty = train one with `feeding embed --train`
    featurization: Literal["bowl_centred", "legacy"] = "bowl_centred"
    window: int = 90
    stride: int = 6
    k_range: tuple[int, int] = (8, 10)


class AnalysisConfig(BaseModel):
    # Conditions included in the statistics, in experimental order; the standard comparisons
    # contrast the first and the last. Empty = every condition, ordered by session number.
    conditions: list[str] = Field(default_factory=list)
    unit: Literal["animal", "bout"] = "animal"
    test: Literal["welch", "student", "mannwhitney"] = "welch"
    correction: Literal["bonferroni", "fdr_bh"] = "fdr_bh"
    seed: int = 0
    standard_view: bool = True  # also run the standard comparisons (groups x first/last condition x contexts)


VIEW_FIELDS = ("group", "condition", "context", "chamber", "animal", "session", "part", "video")


class ViewGroup(BaseModel):
    """One side of a comparison: the bouts / sessions whose metadata match ``where``.

    ``where`` maps a metadata field to the allowed values; a field left out matches
    anything. E.g. ``{group: [Treated], condition: [Post], context: [A]}``.
    """

    label: str
    where: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("where", mode="before")
    @classmethod
    def _listify(cls, v):
        out = {}
        for k, vals in (v or {}).items():
            if k not in VIEW_FIELDS:
                raise ValueError(f"unknown field {k!r}; use one of {', '.join(VIEW_FIELDS)}")
            vals = vals if isinstance(vals, (list, tuple)) else [vals]
            if vals:
                out[k] = [str(x) for x in vals]
        return out


class View(BaseModel):
    """A named comparison of two or more groups (see ``feeding/views.py``)."""

    name: str
    description: str = ""
    groups: list[ViewGroup]
    pairs: list[tuple[str, str]] | None = None  # which groups to test against each other; default all pairs
    paired: Literal["auto", "yes", "no"] = "auto"  # test pairs as repeated measures over shared animals

    @field_validator("paired", mode="before")
    @classmethod
    def _yaml_bool(cls, v):
        return {True: "yes", False: "no"}.get(v, v) if isinstance(v, bool) else v  # YAML reads yes/no as booleans

    @model_validator(mode="after")
    def _check(self) -> "View":
        labels = [g.label for g in self.groups]
        if len(labels) < 2:
            raise ValueError(f"view {self.name!r} needs at least two groups")
        if len(set(labels)) != len(labels):
            raise ValueError(f"view {self.name!r} has duplicate group labels")
        for a, b in self.pairs or []:
            if a not in labels or b not in labels or a == b:
                raise ValueError(f"view {self.name!r}: pair ({a}, {b}) must name two different groups of {labels}")
        return self

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", self.name).strip("-").lower() or "view"

    def pair_list(self) -> list[tuple[str, str]]:
        if self.pairs:
            return [tuple(p) for p in self.pairs]
        labels = [g.label for g in self.groups]
        return [(a, b) for i, a in enumerate(labels) for b in labels[i + 1:]]


class Config(BaseModel):
    name: str | None = None
    paths: Paths = Paths()
    naming: NamingConfig = NamingConfig()
    sleap: SleapConfig = SleapConfig()
    split: SplitConfig = SplitConfig()
    skeleton: SkeletonConfig = SkeletonConfig()
    quality: QualityConfig = QualityConfig()
    bouts: BoutConfig = BoutConfig()
    features: FeatureConfig = FeatureConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    views: list[View] = Field(default_factory=list)

    source: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _nodes_exist(self) -> "Config":
        if self.features.speed_nodes is None:
            self.features.speed_nodes = self.tracked_nodes
        if self.features.body_node is None and self.features.speed_nodes:
            nodes = self.features.speed_nodes
            self.features.body_node = next((n for n in BODY_NODES if n in nodes), nodes[0])
        slugs = [v.slug for v in self.views]
        if len(set(slugs)) != len(slugs):
            raise ValueError("views need distinct names")
        if not self.skeleton.nodes:
            return self  # no model/predictions yet: nothing to check
        known = set(self.skeleton.nodes)
        refs = {
            "bouts.node": [self.bouts.node],
            "quality.drop_nodes": self.quality.drop_nodes,
            "features.speed_nodes": self.features.speed_nodes,
            "features.body_node": [self.features.body_node] if self.features.body_node else [],
            "skeleton.edges": [n for e in self.skeleton.edges for n in e],
        }
        bad = {k: sorted(set(v) - known) for k, v in refs.items() if set(v) - known}
        if bad:
            raise ValueError(f"node names not in skeleton.nodes {self.skeleton.nodes}: {bad}")
        return self

    @property
    def root(self) -> Path:
        """The project folder."""
        assert self.source is not None
        return self.source.parent

    @property
    def project_name(self) -> str:
        return self.name or self.root.name

    @property
    def tracked_nodes(self) -> list[str]:
        """Nodes used for filtering and features (skeleton minus dropped nodes)."""
        return [n for n in self.skeleton.nodes if n not in self.quality.drop_nodes]

    def analysis_hash(self) -> str:
        """Hash of every parameter that affects derived data (excludes paths)."""
        payload = self.model_dump(mode="json", exclude={"name", "paths", "naming", "sleap", "split", "embedding", "views"})
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def _resolve(base: Path, p: Path | str) -> Path:
    p = Path(os.path.expanduser(str(p)))
    return p if p.is_absolute() else (base / p).resolve()


def skeleton_from_model(model_dir: Path) -> dict:
    """Node names (and edges, if stored) from a SLEAP model's training_config.json.

    Pure JSON parsing, so it works without the SLEAP environment. Node objects
    are jsonpickle-encoded; their first occurrence carries the name.
    """
    raw = json.loads((Path(model_dir) / "training_config.json").read_text(encoding="utf-8"))
    sk = raw["data"]["labels"]["skeletons"][0]
    nodes: list[str] = []
    by_id: dict[int, str] = {}
    counter = [0]

    def walk(o):
        if isinstance(o, dict):
            if "py/object" in o:
                counter[0] += 1
                if o["py/object"] == "sleap.skeleton.Node":
                    name = o["py/state"]["py/tuple"][0] if "py/tuple" in o.get("py/state", {}) else o["py/state"]["name"]
                    by_id[counter[0]] = name
                    if name not in nodes:
                        nodes.append(name)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(sk)
    edges = []
    for link in sk.get("links", []):
        ends = []
        for end in (link.get("source"), link.get("target")):
            if isinstance(end, dict) and "py/id" in end:
                ends.append(by_id.get(end["py/id"]))
            elif isinstance(end, dict) and "py/state" in end:
                st = end["py/state"]
                ends.append(st["py/tuple"][0] if "py/tuple" in st else st.get("name"))
        if len(ends) == 2 and all(ends):
            edges.append(ends)
    heads = [k for k, v in raw.get("model", {}).get("heads", {}).items() if v is not None]
    return {"nodes": nodes, "edges": edges, "heads": heads}


def _skeleton_from_predictions(pred_dir: Path) -> list[str]:
    import h5py

    for h5 in sorted(Path(pred_dir).glob("*.analysis.h5"))[:1]:
        with h5py.File(h5, "r") as f:
            return [n.decode() if isinstance(n, bytes) else str(n) for n in f["node_names"][:]]
    return []


def find_project(start: Path | None = None) -> Path:
    """Path of the project's feeding.yaml (see module docstring for the search order)."""
    env = os.environ.get("FEEDING_PROJECT") or os.environ.get("FEEDING_CONFIG")
    if start is None and env:
        start = Path(env)
    if start is not None:
        p = Path(start).expanduser().resolve()
        if p.is_dir():
            p = p / PROJECT_CONFIG_NAME
        if not p.exists():
            raise NoProjectError(f"no {PROJECT_CONFIG_NAME} at {p.parent}")
        return p
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / PROJECT_CONFIG_NAME).exists():
            return d / PROJECT_CONFIG_NAME
    raise NoProjectError(
        f"not inside a project (no {PROJECT_CONFIG_NAME} here or in a parent folder); "
        "pass --project DIR, or create one with `feeding init DIR`"
    )


def load_project(path: str | Path | None = None) -> Config:
    """Load a project from its folder or its feeding.yaml (or discover it)."""
    cfg_path = find_project(Path(path) if path else None)
    base = cfg_path.parent
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    sleap = raw.setdefault("sleap", {}) or {}
    raw["sleap"] = sleap
    if not sleap.get("models"):
        # Convention: a single model folder inside <project>/models is the default model.
        found = sorted(d for d in (base / "models").glob("*") if (d / "training_config.json").exists())
        if len(found) == 1:
            sleap["models"] = {"default": str(found[0])}
    if not (raw.get("skeleton") or {}).get("nodes"):
        # Skeleton from the first model, else from existing predictions, else empty for now.
        sk = {"nodes": [], "edges": []}
        for v in (sleap.get("models") or {}).values():
            first = _resolve(base, v[0] if isinstance(v, list) else v)
            if (first / "training_config.json").exists():
                sk = skeleton_from_model(first)
                break
        if not sk["nodes"]:
            pred_dir = _resolve(base, (raw.get("paths") or {}).get("predictions", Paths().predictions))
            sk["nodes"] = _skeleton_from_predictions(pred_dir) if pred_dir.is_dir() else []
        raw["skeleton"] = {"nodes": sk["nodes"], "edges": sk.get("edges", [])}
        if sk["nodes"] and (raw.get("bouts") or {}).get("node") is None and "Snout" not in sk["nodes"]:
            raw.setdefault("bouts", {})["node"] = sk["nodes"][0]
    cfg = Config.model_validate(raw)
    for name in Paths.model_fields:
        v = getattr(cfg.paths, name)
        setattr(cfg.paths, name, [_resolve(base, x) for x in v] if isinstance(v, list) else _resolve(base, v))
    if cfg.split.raw_videos is not None:
        cfg.split.raw_videos = _resolve(base, cfg.split.raw_videos)
    cfg.split.output = _resolve(base, cfg.split.output)
    if cfg.embedding.checkpoint is not None:
        cfg.embedding.checkpoint = _resolve(base, cfg.embedding.checkpoint)
    if cfg.sleap.bin_dir is None:
        from .settings import find_sleap_bin

        cfg.sleap.bin_dir = find_sleap_bin()
    else:
        cfg.sleap.bin_dir = _resolve(base, cfg.sleap.bin_dir)
    cfg.sleap.models = {
        k: [_resolve(base, x) for x in v] if isinstance(v, list) else _resolve(base, v)
        for k, v in cfg.sleap.models.items()
    }
    cfg.source = cfg_path
    return cfg


load_config = load_project  # backwards-compatible name
