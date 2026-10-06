# Output files and reproducibility

All generated files are under the project's `data/` folder (the locations can be changed under
[`paths`](12-configuration.md#paths)).

```
data/
  predictions/   <video>.slp, <video>.analysis.h5, <video>.provenance.json
  derived/       cached per-video tracks (parquet); safe to delete, recomputed when needed
  results/       one folder per analysis run (and per embedding run)
  logs/          logs of jobs started in the app
```

## An analysis run

Each run creates `data/results/<YYYYMMDD-HHMMSS>_<settings hash>/`:

```
manifest.json          what was run, on what, with which settings and code (below)
feeding.yaml           a copy of the project configuration used
bouts.csv              one row per bout: frame indices of the windows and contacts
bout_features.csv      one row per bout: metadata + bout features
sessions.csv           one row per video: metadata + session and locomotion measures
occupancy.npz          per-video 2-D histogram of the contact keypoint around the bowl
occupancy_rims.json    each video's rim, centred on the bowl, for drawing the maps
views/                 one folder per comparison (below), and index.json summarising them
exploratory/           pca.csv, umap.csv, local_clustering_*.csv, rf_conditions.csv, summary.json, figures
exploratory/per_animal/<feature>.png   per-animal box plots
figures/heatmaps/animal<ID>_<context>.png
figures/tornado_distance/animal<ID>_<context>.png
figures/tornado_speed/animal<ID>_<context>.png
```

### Tables

Every table carries each video's metadata: `video`, `session`, `part`, `condition`, `context`, `animal`, `chamber`
and `group`. Feature definitions are in [Running the analysis](08-analysis.md#bout-features). `bouts.csv` has, per
bout:

| Column | Meaning |
|---|---|
| `window_start` | first frame of the approach |
| `contact_start` | first contact frame |
| `contact_end` | frame after the last contact |
| `window_end` | frame after the end of the withdrawal |
| `n_episodes` | contact episodes in the bout |
| `contact_frames` | frames in contact |
| `longest_episode` | longest continuous contact |
| `segment` | the tracked stretch the bout lies in |

Frame indices are 0-based.

### Comparison folders

`views/<comparison>/`:

| File | Contents |
|---|---|
| `view.json` | the comparison's definition, the statistics settings, group sizes and when it ran |
| `groups.csv` | what each group matched: animals (and IDs), sessions, videos, bouts |
| `stats_bouts.csv` | every pair × bout feature (see [The statistics tables](11-statistics.md#the-statistics-tables)) |
| `stats_sessions.csv` | every pair × session/locomotion measure |
| `omnibus_bouts.csv`, `omnibus_sessions.csv` | the all-groups test (three or more groups, all pairs) |
| `stats.xlsx` | all of the above as sheets, plus one sheet per pair sorted by p |
| `figures/` | `overview_*.png`, `boxplots_bouts/`, `boxplots_sessions/`, `occupancy.png`, `pca.png` |

### manifest.json

| Key | Contents |
|---|---|
| `created_at`, `elapsed_s` | when the run happened and how long it took |
| `project` | name and folder |
| `analysis_hash`, `config` | the settings hash and the full settings |
| `subjects_sha256` | hash of the groups table |
| `git` | commit of the code and whether it had uncommitted changes |
| `packages` | versions of the Python packages |
| `inputs` | per video: predictions hash and source, model, SLEAP version, bowl annotation and rim |
| `skipped` | videos not analysed, and why |
| `notes` | analyses that could not be done, and why |
| `n_videos_analysed`, `n_bouts`, `outputs` | summary |

## Reproducing a result

A run is determined by:

- the code (`git` in the manifest; `uv.lock` pins every package);
- the settings (`feeding.yaml` copied into the run);
- the groups (`subjects_sha256`);
- the bowl annotations (copied into the manifest);
- the predictions (hashed in the manifest, and traced to model and SLEAP version by their provenance files).

To reproduce a run:

1. Check out the commit recorded in the manifest and run `uv sync`.
2. Use the run's `feeding.yaml`, the same groups table, bowl annotations and predictions.
3. Run `feeding analyze`.

Everything random is seeded, so the numbers are identical. Prediction files are matched by hash, so you can verify
that the poses are the same ones.

SLEAP inference itself is reproducible from the provenance files, which record the model weights' hashes, the SLEAP
version and the exact command. Results can differ slightly between GPU and CPU, or between GPU models, which is why
the predictions themselves are hashed.

## Sharing results

- **Export all (.zip)** (Results page) or the run folder itself contains everything about one run.
- **Download (.zip)** on a comparison contains just that comparison.
- The **Excel** workbook of a comparison is convenient for supplementary tables.
- Figures are PNG files at 200 dpi. Fonts and colours are consistent across figures, and the colour palette was
  chosen to stay distinguishable for colour-blind readers. For a journal's exact style, redraw from the CSV tables.
