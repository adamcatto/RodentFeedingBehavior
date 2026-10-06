# Installation

The tool is a Python package with a browser interface. It is installed with
[uv](https://docs.astral.sh/uv/), which also installs a suitable Python for you. SLEAP, which does the pose
estimation, is installed separately in its own environment; you only need it to run inference yourself.

## 1. Install uv

**macOS and Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new terminal afterwards so that the `uv` command is found.

## 2. Get the tool

```bash
git clone https://github.com/adamcatto/RodentFeedingBehavior.git
cd RodentFeedingBehavior
uv sync
```

`uv sync` creates a virtual environment in `.venv/` with the exact package versions pinned in `uv.lock`, so everyone
gets the same results. Optional extras:

```bash
uv sync --extra umap     # UMAP projections in the exploratory analyses
uv sync --extra embed    # LSTM-VAE behaviour embedding (installs PyTorch)
uv sync --all-extras     # both
```

Check that it works:

```bash
uv run feeding --help
uv run pytest            # optional: the test suite (about 30 seconds)
```

> **Without git:** download the repository as a zip from GitHub, unpack it, and run `uv sync` inside the folder.
>
> **With pip instead of uv:** `pip install "git+https://github.com/adamcatto/RodentFeedingBehavior.git"` installs
> the `feeding` command into the active Python environment (versions are then not pinned by `uv.lock`).

In this manual, commands are written as `feeding …`. From a source checkout, run them as `uv run feeding …` inside the
repository folder (or activate `.venv` first).

## 3. Start the app

```bash
uv run feeding serve
```

Then open <http://127.0.0.1:8765> in your browser. The server keeps running in the terminal; stop it with
<kbd>Ctrl</kbd>+<kbd>C</kbd>. Useful options:

```bash
uv run feeding serve path/to/project     # open this project straight away
uv run feeding serve --port 9000         # another port
```

The server only listens on your own computer (`127.0.0.1`) by default. Native file dialogs (*Browse…*, *Choose
files…*) open on the computer that runs the server.

> **After updating the code** (e.g. `git pull`), restart `feeding serve`. If you don't, the page shows a red banner
> saying that the server is older than the app.

To try everything without SLEAP or your own data, continue with the [Quick start](03-quick-start.md).

## 4. Install SLEAP (for running inference)

SLEAP has its own, strict dependencies, so it lives in a separate conda environment, and this tool calls its
command-line programs (`sleap-track`, `sleap-convert`). Follow the
[SLEAP installation guide](https://sleap.ai/installation.html). It typically comes down to installing
[Miniforge](https://github.com/conda-forge/miniforge) or Miniconda and then:

```bash
conda create -y -n sleap -c conda-forge -c nvidia -c sleap -c anaconda sleap
```

The tool finds SLEAP automatically when the environment is called `sleap` (or starts with `sleap`) and lives in a
usual conda location (`~/miniconda3`, `~/miniforge3`, `~/anaconda3`, `~/mambaforge`, `~/opt/…`; on Windows also under
`AppData\Local` and `C:\ProgramData`), or when `sleap-track` is on your `PATH`. Otherwise tell it where SLEAP is in
either of these ways:

- **in the app:** *Settings → SLEAP inference → SLEAP environment*, set to the environment's `bin` folder (on Windows,
  its `Scripts` folder);
- **from a terminal:** set `FEEDING_SLEAP_BIN=/path/to/envs/sleap/bin` (in PowerShell:
  `$env:FEEDING_SLEAP_BIN = "C:\Users\me\miniforge3\envs\sleap\Scripts"`).

The tool runs SLEAP's programs as if its environment were activated, so you don't need to activate it yourself.

`feeding check` and the app's warning bar report when `sleap-track` cannot be found. The pipeline has been tested with
SLEAP 1.2 and works with the same command-line options in later 1.x versions.

You also need a **trained SLEAP model** for your setup: a single-animal model (`single_instance`) or a top-down pair
(`centroid` + `centered_instance`). Train it in SLEAP's own GUI. See [SLEAP pose estimation](05-sleap.md).

## Windows

The tool supports Windows 10 and 11. A few notes:

- **Terminal.** Run the commands in PowerShell or Windows Terminal. `uv run feeding …` works the same as on macOS and
  Linux.
- **git.** Install it with `winget install Git.Git` (or from <https://git-scm.com>), or download the repository as a
  zip from GitHub instead.
- **Paths.** Type Windows paths as usual (`D:\videos\cohort1`). Paths inside a project are stored with `/`, so a
  project folder can be moved between Windows, macOS and Linux.
- **Browse buttons** open the Windows file and folder dialogs. The in-page folder browser (used when the server runs
  on another computer) has a drive selector.
- **SLEAP** runs on Windows with an NVIDIA GPU, or on the CPU. Install it in a conda environment as above; the tool
  finds `sleap-track.exe` in the environment's `Scripts` folder.
- **ffmpeg** (only for `feeding split`): `winget install Gyan.FFmpeg`, then open a new terminal.

## Where things are kept

| What | Default location | Change with |
|---|---|---|
| New projects | `projects/` in the repository (or `~/feeding-behavior/projects` for a pip install) | `FEEDING_PROJECTS_DIR` |
| Model library (models offered in pickers) | `models/` in the repository (or `~/feeding-behavior/models`) | `FEEDING_MODEL_LIBRARY` (several folders separated by `:`; `;` on Windows) |
| Recently opened projects | `data/recent_projects.json` in the same app folder | `FEEDING_HOME` moves the whole app folder |
| SLEAP | found automatically | `FEEDING_SLEAP_BIN`, or *Settings* |

Projects can live anywhere: the `projects/` folder is only where *New project* puts them by default. In a source
checkout, `projects/` and `models/` are ignored by git, so your data never ends up in the code repository.

## Updating

```bash
git pull
uv sync
```

Then restart `feeding serve`. Projects keep working across updates; a new run records the code version it used.
