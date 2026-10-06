# Configuration reference (feeding.yaml)

Every project setting is in the project's `feeding.yaml`. The app's **Settings** page and the comparison editor edit
the common ones. For everything else, edit the file in a text editor, or use `feeding config KEY VALUE`, which
validates the change and keeps the comments.

- Every setting has a default, so a minimal file can be nearly empty.
- Relative paths are relative to the project folder.
- An invalid file is reported with the offending key, both when the app opens it and by `feeding check`.

```bash
feeding config                          # show every setting (with defaults filled in)
feeding config bouts                    # one section
feeding config bouts.margin_px 2        # change one value
feeding config analysis.conditions "[Pre, Post]"
feeding config sleap.bin_dir null       # remove a value (back to the default)
```

The settings that change derived data are hashed: the hash is part of every run's folder name. These are `skeleton`,
`quality`, `bouts`, `features` and `analysis`. A run can therefore always be matched to its settings, and cached
per-video results are recomputed when the settings change.

## name

The project's display name (default: the folder name).

## paths

| Key | Default | Meaning |
|---|---|---|
| `videos` | `[videos]` | one or more folders of videos (read-only input; may be outside the project) |
| `predictions` | `data/predictions` | SLEAP outputs and provenance per video |
| `bowls` | `annotations/bowls` | bowl annotations (one JSON per video) |
| `subjects` | `metadata/subjects.csv` | animal → group table |
| `derived` | `data/derived` | cached per-video tracks (safe to delete) |
| `results` | `data/results` | one folder per analysis run |
| `logs` | `data/logs` | logs of jobs started in the app |

## naming

| Key | Default | Meaning |
|---|---|---|
| `pattern` | `{session}[_{part}]_{condition}_{context}_{animal}` (see below) | regular expression matched against each video's file name (without extension) |
| `chambers` | `{}` | display label per context, e.g. `{A: square arena, B: round arena}` |

The default pattern is
`^(?P<session>\d+)(?:_(?P<part>\d+))?_(?P<condition>[A-Za-z]+)_(?P<context>[A-Za-z0-9]+)_(?P<animal>[A-Za-z0-9]+)$`.
See [Naming videos](04-projects.md#naming-videos).

## sleap

| Key | Default | Meaning |
|---|---|---|
| `bin_dir` | `null` (find automatically) | `bin` folder of the SLEAP conda environment |
| `models` | `{}` | model folder per context, or `default`; a list for a top-down pair |
| `batch_size` | 4 | frames per batch |
| `peak_threshold` | 0.2 | minimum keypoint confidence (0–1) |
| `device` | `auto` | `auto`, `cpu`, `gpu` or a GPU index |
| `tracker` | `none` | `none`, `simple` or `flow` |
| `target_instance_count` | 0 | tracker: animals per frame (0 = no limit) |
| `pre_cull_to_target` | false | tracker: drop extra instances before tracking |
| `post_connect_single_breaks` | false | tracker: join single track breaks |
| `clean_instance_count` | 0 | tracker: instances kept after tracking (0 = off) |
| `similarity` | `instance` | tracker: `instance`, `centroid` or `iou` |
| `match` | `greedy` | tracker: `greedy` or `hungarian` |
| `track_window` | 5 | tracker: frames to look back |
| `extra_args` | `[]` | further `sleap-track` arguments, passed verbatim (they override the options above) |

```yaml
sleap:
  models:
    A: models/square.single_instance.n=250
    B: models/round.single_instance.n=270
  # top-down pair for every context instead:
  # models: {default: [models/m.centroid.n=200, models/m.centered_instance.n=200]}
```

See [SLEAP pose estimation](05-sleap.md#inference-parameters).

## skeleton

| Key | Default | Meaning |
|---|---|---|
| `nodes` | read from the model, or from existing predictions | keypoint names, in SLEAP's order |
| `edges` | read from the model | pairs of keypoints joined when drawing |

The skeleton must match the predictions. Every keypoint named in other settings must be one of `nodes`.

## quality

| Key | Default | Meaning |
|---|---|---|
| `drop_nodes` | `[]` | keypoints ignored for filtering and features |
| `score_window` | 300 | frames in the centred rolling mean of each keypoint's score |
| `min_score` | 0.5 | a frame is tracked when every keypoint's rolling-mean score is at least this |
| `min_segment_frames` | 90 | shortest tracked stretch that is kept |
| `max_interp_gap` | 0 | linearly fill gaps of missing coordinates up to this many frames |

See [Quality filtering](07-tracking.md#quality-filtering).

## bouts

| Key | Default | Meaning |
|---|---|---|
| `node` | `Snout` (else the first keypoint) | keypoint that defines contact with the bowl |
| `margin_px` | 0 | grow (+) or shrink (−) the rim for the contact test |
| `min_contact_frames` | 15 | shortest continuous contact that makes a bout |
| `merge_gap_frames` | 90 | shorter away-periods are returns within one bout |
| `flank_frames` | 90 | approach and withdrawal window length |

See [Contact and bouts](08-analysis.md#contact-and-bouts).

## features

| Key | Default | Meaning |
|---|---|---|
| `speed_nodes` | every tracked keypoint | keypoints whose speeds are bout features |
| `body_node` | a body-centre keypoint (`MidBack`, `Centroid`, `Center`, `Body`, `Thorax`, `Spine`, …), else the first speed keypoint | keypoint used for whole-session locomotion |
| `moving_speed` | 1.0 | px/frame (after 5-frame smoothing) above which the animal counts as moving |

## analysis

| Key | Default | Meaning |
|---|---|---|
| `conditions` | `[]` (every condition, in session order) | conditions included in statistics, in experimental order; the standard comparison contrasts the first and the last |
| `unit` | `animal` | `animal` (mean per animal) or `bout` (every bout a sample) |
| `test` | `welch` | `welch`, `student` or `mannwhitney` |
| `correction` | `fdr_bh` | `fdr_bh` or `bonferroni` |
| `seed` | 0 | seed for everything random |
| `standard_view` | true | include the standard comparison in every run |

See [Statistical methods](11-statistics.md).

## views

A list of comparisons; see [Comparisons](10-comparisons.md#in-feedingyaml).

| Key | Meaning |
|---|---|
| `name` | unique name (also the folder name, simplified) |
| `description` | optional text |
| `groups` | list of `{label, where}`; `where` maps fields (`group`, `condition`, `context`, `chamber`, `animal`, `session`, `part`, `video`) to lists of allowed values |
| `pairs` | optional list of `[label, label]` pairs to test (default: all pairs) |
| `paired` | `auto` (default), `yes` or `no` |

## split

Settings for `feeding split`, which splits recordings of four arenas into one video per animal (optional).

| Key | Default | Meaning |
|---|---|---|
| `raw_videos` | `null` | folder of recordings named `{prefix}_{TL}_{TR}_{BL}_{BR}` |
| `output` | `data/split_videos` | where the per-animal videos are written |
| `crf` | 18 | H.264 quality (lower = better, larger files) |
| `gop` | 30 | keyframe interval; small values make frame seeking fast and exact |

## embedding

Settings for the optional behaviour embedding; see [Behaviour embedding](15-embedding.md).

| Key | Default | Meaning |
|---|---|---|
| `checkpoint` | `null` | trained model to use (`model.pt` from `feeding embed --train`) |
| `featurization` | `bowl_centred` | `bowl_centred` or `legacy` (for checkpoints trained on absolute coordinates) |
| `window` | 90 | frames per window |
| `stride` | 6 | embed every *stride*-th window |
| `k_range` | `[8, 10]` | k-means k in [first, last); the best silhouette is kept |

## A complete example

```yaml
name: Feeding after treatment, cohort 2
paths:
  videos: [videos, /mnt/archive/cohort2/videos]
naming:
  pattern: '^(?P<session>\d+)_(?P<condition>[A-Za-z]+)_(?P<context>[AB])_(?P<animal>m\d+)$'
  chambers: {A: square arena, B: round arena}
sleap:
  models: {default: models/mouse.single_instance.n=300}
  batch_size: 8
quality:
  drop_nodes: [TailTip]
bouts:
  node: Snout
  margin_px: 2
analysis:
  conditions: [Pre, Post]
  unit: animal
  test: welch
views:
  - name: Control vs Treated after treatment
    groups:
      - {label: Control, where: {group: [Control], condition: [Post]}}
      - {label: Treated, where: {group: [Treated], condition: [Post]}}
```
