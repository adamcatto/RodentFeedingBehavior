"""App-wide defaults: where new projects go, where SLEAP models and SLEAP itself are found.

The *app folder* is the repository when running from a source checkout, else
``~/feeding-behavior`` (or ``FEEDING_HOME``). Within it:

- Projects folder: ``FEEDING_PROJECTS_DIR``, else ``<app folder>/projects``.
- Model library: ``FEEDING_MODEL_LIBRARY`` (folders separated by ``:``, ``;`` on
  Windows), else ``<app folder>/models``. Each library folder contains SLEAP model
  folders (or links to them); a project's own ``models/`` folder is always searched too.
- Recently opened projects: ``<app folder>/data/recent_projects.json``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import PROJECT_ROOT, skeleton_from_model


def app_home() -> Path:
    env = os.environ.get("FEEDING_HOME")
    if env:
        return Path(env).expanduser()
    if (PROJECT_ROOT / "pyproject.toml").exists() and (PROJECT_ROOT / "src" / "feeding").is_dir():
        return PROJECT_ROOT  # source checkout
    return Path.home() / "feeding-behavior"


def projects_dir() -> Path:
    return Path(os.environ.get("FEEDING_PROJECTS_DIR", app_home() / "projects")).expanduser()


def recent_file() -> Path:
    return Path(os.environ.get("FEEDING_RECENT_FILE", app_home() / "data" / "recent_projects.json")).expanduser()


def model_library() -> list[Path]:
    env = os.environ.get("FEEDING_MODEL_LIBRARY")
    dirs = env.split(os.pathsep) if env else [str(app_home() / "models")]
    return [Path(d).expanduser() for d in dirs if d]


def is_model_dir(p: Path) -> bool:
    return (Path(p) / "training_config.json").exists()


def find_models(folders: list[Path]) -> list[dict]:
    """SLEAP model folders inside ``folders`` (one level deep; links followed)."""
    out, seen = [], set()
    for folder in folders:
        if not folder.is_dir():
            continue
        candidates = [folder] if is_model_dir(folder) else sorted(c for c in folder.iterdir() if c.is_dir())
        for c in candidates:
            if not is_model_dir(c) or c.resolve() in seen:
                continue
            seen.add(c.resolve())
            try:
                sk = skeleton_from_model(c)
            except (KeyError, ValueError, OSError):
                sk = {"nodes": [], "heads": []}
            out.append({"name": c.name, "path": str(c), "resolved": str(c.resolve()), "library": str(folder),
                        "nodes": sk["nodes"], "heads": sk["heads"]})
    return out


# Where conda / mamba usually live; a SLEAP environment is looked for as <base>/envs/sleap*.
_CONDA_BASES = ["~/miniconda3", "~/anaconda3", "~/miniforge3", "~/mambaforge", "~/micromamba", "~/opt/miniconda3",
                "~/opt/anaconda3", "~/.conda", "/opt/conda", "/opt/miniconda3", "/opt/homebrew/Caskroom/miniforge/base",
                "~/AppData/Local/miniconda3", "~/AppData/Local/anaconda3", "~/AppData/Local/miniforge3",
                "C:/ProgramData/miniconda3", "C:/ProgramData/Anaconda3", "C:/ProgramData/miniforge3"]


def sleap_exe(bin_dir: Path | None, name: str) -> Path | None:
    """``name`` (sleap-track, sleap-convert, python) in a SLEAP environment's bin folder, if present.
    On Windows, scripts are in ``Scripts\\`` and python.exe one level up. The environment folder
    itself is accepted too."""
    if bin_dir is None:
        return None
    bin_dir = Path(bin_dir)
    for c in (bin_dir / name, bin_dir / f"{name}.exe", bin_dir.parent / f"{name}.exe",
              bin_dir / "bin" / name, bin_dir / "Scripts" / f"{name}.exe"):
        if c.is_file():
            return c
    return None


def sleap_env(bin_dir: Path | None) -> dict[str, str]:
    """Environment for SLEAP's programs, as if its conda environment were activated: its folders
    first on PATH (on Windows, the TensorFlow and CUDA DLLs in ``Library\\bin`` are only found
    through PATH), and UTF-8 output."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if bin_dir is None:
        return env
    b = Path(bin_dir)
    prefix = b.parent if b.name.lower() in ("bin", "scripts") else b
    if os.name == "nt":
        lib = prefix / "Library"
        dirs = [prefix, lib / "mingw-w64" / "bin", lib / "usr" / "bin", lib / "bin", prefix / "Scripts", prefix / "bin"]
    else:
        dirs = [prefix / "bin"]
    env["PATH"] = os.pathsep.join([str(d) for d in dirs if d.is_dir()] + [env.get("PATH", "")])
    return env


def find_sleap_bin() -> Path | None:
    """The SLEAP environment's bin folder: ``FEEDING_SLEAP_BIN``, else ``sleap-track`` on the
    PATH, else a conda environment named ``sleap*`` in the usual places (None if not found)."""
    env = os.environ.get("FEEDING_SLEAP_BIN")
    if env:
        return Path(env).expanduser()
    found = shutil.which("sleap-track")
    if found:
        return Path(found).parent
    bases = [os.environ.get("CONDA_EXE", ""), os.environ.get("MAMBA_ROOT_PREFIX", "")]
    bases = [str(Path(b).parents[1]) if b.endswith(("conda", "conda.exe")) else b for b in bases if b] + _CONDA_BASES
    for base in bases:
        envs = Path(base).expanduser() / "envs"
        if not envs.is_dir():
            continue
        for e in sorted(envs.glob("sleap*")):
            for sub in ("bin", "Scripts"):
                if sleap_exe(e / sub, "sleap-track"):
                    return e / sub
    return None
