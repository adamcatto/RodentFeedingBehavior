"""Parsing of per-animal video names and subject metadata.

Video file stems are matched against ``naming.pattern`` (a regex with named
groups). The default pattern is ``{session}[_{part}]_{condition}_{context}_{animal}``,
e.g. ``3_Pre_A_m12`` or ``7_2_Post_B_m4`` (part 2 of a session recorded in
several files). ``context`` is typically the arena or chamber; ``naming.chambers``
can give each context a display label.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .config import DEFAULT_NAME_PATTERN

VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mpg", ".mkv")
NA = "NA"


@dataclass(frozen=True)
class VideoInfo:
    name: str
    session: str | None
    part: str | None
    condition: str
    context: str
    animal: str

    def as_dict(self, chambers: dict[str, str] | None = None) -> dict:
        return {**asdict(self), "chamber": (chambers or {}).get(self.context)}


@lru_cache(maxsize=8)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def parse_video_name(name: str, pattern: str = DEFAULT_NAME_PATTERN) -> VideoInfo:
    stem = Path(name).stem if Path(name).suffix.lower() in VIDEO_EXTENSIONS else name
    m = _compile(pattern).match(stem)
    if not m:
        raise ValueError(f"Video name {stem!r} does not match naming.pattern {pattern!r}")
    d = m.groupdict()
    return VideoInfo(
        name=stem,
        session=d.get("session"),
        part=d.get("part"),
        condition=d.get("condition") or NA,
        context=d.get("context") or NA,
        animal=d["animal"],
    )


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def video_files(videos_dirs: Path | list[Path]) -> list[Path]:
    """Every video file in the given folder(s), whatever its name (first folder wins on duplicates)."""
    dirs = [videos_dirs] if isinstance(videos_dirs, (str, Path)) else videos_dirs
    out: dict[str, Path] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith(".") and p.stem not in out:
                out[p.stem] = p
    return sorted(out.values(), key=lambda p: natural_key(p.stem))


def list_videos(videos_dirs: Path | list[Path], pattern: str = DEFAULT_NAME_PATTERN) -> list[Path]:
    """Videos in the given folder(s) whose names match ``pattern``, naturally sorted."""
    out = []
    for p in video_files(videos_dirs):
        try:
            parse_video_name(p.stem, pattern)
        except ValueError:
            continue
        out.append(p)
    return out


def load_subjects(path: Path) -> pd.DataFrame:
    """Animal -> group table (columns: animal, group). Animal IDs are strings."""
    df = pd.read_csv(path, dtype={"animal": str, "group": str})
    missing = {"animal", "group"} - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing columns {missing}")
    return df


def video_metadata(names: list[str], subjects: pd.DataFrame, pattern: str = DEFAULT_NAME_PATTERN,
                   chambers: dict[str, str] | None = None) -> pd.DataFrame:
    """One row per video with parsed name fields joined to subject group."""
    rows = [parse_video_name(n, pattern).as_dict(chambers) for n in names]
    df = pd.DataFrame(rows, columns=["name", "session", "part", "condition", "context", "animal", "chamber"])
    return df.merge(subjects, on="animal", how="left").rename(columns={"name": "video"})
