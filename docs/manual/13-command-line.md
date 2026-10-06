# Command-line reference

Everything the app does can also be done with the `feeding` command, e.g. to process many videos on a server without
a browser, or in scripts. From a source checkout, run it as `uv run feeding …` in the repository folder.

**Choosing the project.** Commands act on one project, chosen in this order:

1. `--project DIR` (or `-p DIR`), a project folder or its `feeding.yaml`;
2. the `FEEDING_PROJECT` environment variable;
3. the current folder, or the nearest parent folder that contains a `feeding.yaml` (like git).

For example, both of these work:

```bash
uv run feeding status -p projects/my-cohort
cd projects/my-cohort && uv run --project ../.. feeding status
```

`feeding COMMAND --help` describes every option of a command.

## Overview

| Command | Purpose |
|---|---|
| `feeding serve [PROJECT]` | start the browser app |
| `feeding demo [DIR]` | create the synthetic demo project |
| `feeding init DIR` | create a project |
| `feeding add-videos FILES_OR_FOLDERS…` | add videos (files are copied, folders used in place) |
| `feeding models` | list the SLEAP models in the model library |
| `feeding set-model MODEL…` | assign SLEAP model(s) |
| `feeding config [KEY [VALUE]]` | show or change settings |
| `feeding check` | check the project for problems |
| `feeding status` | list videos with their prediction and bowl status |
| `feeding infer VIDEO… / --all` | run SLEAP inference |
| `feeding import-predictions DIR…` | import existing SLEAP analysis files |
| `feeding split` | split four-arena recordings into one video per animal |
| `feeding analyze [VIDEO…]` | run the analysis |
| `feeding compare [RUN]` | (re-)run comparisons on an existing run |
| `feeding embed` | optional behaviour embedding and clustering |

## serve

```bash
feeding serve [PROJECT] [--host 127.0.0.1] [--port 8765]
```

Starts the app at `http://HOST:PORT`. With `PROJECT`, that project is opened; otherwise the project in the current
folder, if any. `--host 0.0.0.0` makes the app reachable from other computers. It has no login, so do this only on a
trusted network.

## demo

```bash
feeding demo [DIR] [--animals 5] [--seconds 120] [--seed 0] [--no-bowls] [--force]
```

Creates the synthetic demo project (default `projects/demo`): two groups of `--animals` animals, four sessions each,
with videos, predictions and (unless `--no-bowls`) bowl annotations. See [Quick start](03-quick-start.md).

## init

```bash
feeding init DIR [--videos FOLDER]… [--model PATH | --model CONTEXT=PATH]… [--pattern REGEX]
                 [--chamber CONTEXT=LABEL]… [--name NAME] [--sleap-bin PATH] [--force]
```

Creates a project folder with `feeding.yaml`, `metadata/subjects.csv` (one row per animal found, groups blank) and
the standard subfolders. Everything except `DIR` is optional:

- the naming pattern is guessed from the video names;
- the skeleton is read from the model;
- `--force` overwrites an existing `feeding.yaml`.

## add-videos

```bash
feeding add-videos FILE_OR_FOLDER… [--move] [-p PROJECT]
```

Video files are copied into `<project>/videos` (moved with `--move`). Folders are added to `paths.videos` and read in
place.

## models, set-model

```bash
feeding models
feeding set-model PATH                     # one model for every video
feeding set-model A=PATH_A B=PATH_B        # one per context
feeding set-model CENTROID CENTERED        # a top-down pair
```

`set-model` replaces the current assignment and updates the skeleton and the keypoint settings to match the model.

## config

```bash
feeding config                     # every setting
feeding config SECTION             # e.g. sleap, bouts, analysis
feeding config KEY VALUE           # e.g. bouts.margin_px 2; VALUE is YAML: 8, true, [a, b], null
```

Changes are validated and written with comments preserved. See [Configuration](12-configuration.md).

## check, status

`feeding check` reports problems (exit code 1 if there are errors), e.g.:

- missing folders or models;
- contexts without a model;
- animals without a group;
- SLEAP not found.

`feeding status` lists every video with its metadata and whether it has predictions and a bowl.

## infer

```bash
feeding infer VIDEO… | --all [--force]
              [--batch-size N] [--peak-threshold X] [--device auto|cpu|gpu|N] [--tracker none|simple|flow]
              [--target-instance-count N] [--sleap-arg ARG]… [--save]
```

Runs SLEAP on the given videos (by name, without extension), or on every video without predictions (`--all`).
`--force` re-runs videos that already have predictions. The options override the project's `sleap:` settings for this
run; `--save` stores them in `feeding.yaml`. The resulting `sleap-track` options are printed first. See
[SLEAP pose estimation](05-sleap.md).

## import-predictions

```bash
feeding import-predictions DIR… [--overwrite]
```

Copies SLEAP analysis files named `<video>.h5` or `<video>.analysis.h5` from the given folders, after checking their
skeleton and length.

## split

```bash
feeding split [--overwrite]
```

Splits recordings of four arenas in a 2×2 grid, found in `split.raw_videos`, into one video per animal in
`split.output`. A recording `<prefix>_<TL>_<TR>_<BL>_<BR>.<ext>` gives `<prefix>_<animal>.mp4` for each of the four
animal IDs (top-left, top-right, bottom-left, bottom-right). The split keeps the source frame rate and uses frequent
keyframes for exact seeking (`split.crf`, `split.gop`). It needs `ffmpeg` on the `PATH`. Then add `split.output` to
the project's videos.

## analyze

```bash
feeding analyze [VIDEO…] [--no-plots]
```

Analyses every ready video (or only the listed ones) and writes a new run folder. See
[Running the analysis](08-analysis.md).

## compare

```bash
feeding compare [RUN] [-v NAME]… [--no-plots]
```

Runs the standard comparison and the project's comparisons on the latest run (or `RUN`, a run folder name). This is
fast: only the run's tables are read. `-v` limits it to the named comparisons. See [Comparisons](10-comparisons.md).

## embed

```bash
feeding embed [--train] [--epochs 50]
```

Optional; needs `uv sync --extra embed`. See [Behaviour embedding](15-embedding.md).

## Environment variables

| Variable | Meaning |
|---|---|
| `FEEDING_PROJECT` | project to use when `--project` is not given (and to open in `serve`) |
| `FEEDING_HOME` | app folder (default: the repository, or `~/feeding-behavior` for a pip install) |
| `FEEDING_PROJECTS_DIR` | where *New project* and `feeding demo` create projects |
| `FEEDING_MODEL_LIBRARY` | model library folders (separated by `:`, or `;` on Windows) |
| `FEEDING_SLEAP_BIN` | the SLEAP environment's `bin` folder |
| `FEEDING_RECENT_FILE` | file listing recently opened projects |
| `FEEDING_NATIVE_DIALOGS=0` | disable the native file dialogs (use the in-page folder browser) |
