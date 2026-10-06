# Troubleshooting and FAQ

## The app

**The page shows a red banner: "The app was updated after this server started".**
The code changed (e.g. after `git pull`) while `feeding serve` was running. Stop the server with
<kbd>Ctrl</kbd>+<kbd>C</kbd>, start it again, and reload the page.

**The page doesn't load, or shows "address already in use".**
Another program uses the port. Start on another one: `feeding serve --port 8800`.

**"Browse…" or "Choose files…" opens an in-page folder browser instead of the system dialog.**
Native dialogs only open on the computer running the server, and need a desktop session (on Linux, `zenity`, `kdialog`
or Tk). Type or browse to the path in the page instead, or drag and drop files onto the Videos drop zone.

**A video is missing from the list.**
Its file name doesn't match the naming pattern. *Settings → Naming* shows which files match and how they are read.
Adjust the pattern, or rename the file.

**Changing a setting has no effect on old results.**
Results are never changed after the fact: run the analysis again. Comparisons can be re-run on an existing run
(*Re-run*, or `feeding compare`), because they only read the run's tables.

## SLEAP

**"sleap-track not found".**
Set *Settings → SLEAP inference → SLEAP environment* to the environment's `bin` folder (`…/envs/sleap/bin`; on
Windows `…\envs\sleap\Scripts`). Alternatively, set `FEEDING_SLEAP_BIN`. Check from a terminal that
`…/bin/sleap-track --help` works.

**Inference fails with an out-of-memory error.**
Lower the batch size (*Settings → SLEAP inference*, or `feeding infer --batch-size 2`). Or run on the CPU
(`--device cpu`), which is slower.

**Inference is very slow.**
Without a GPU, SLEAP runs on the CPU; expect minutes per minute of video. Check that SLEAP sees the GPU. Its log (in
the Jobs drawer) reports the device it uses. Larger batches help on a GPU.

**"model … has nodes [...] but skeleton.nodes is [...]".**
The model's keypoints differ from those of the project's predictions or other models. Use models trained on the same
skeleton, or re-run SLEAP on all videos after changing the model.

**The tracking is poor in some videos.**
Label frames from those videos in SLEAP (it has a *suggestions* feature for this), retrain, and re-run the affected
videos. Lighting changes, a different camera angle or a new arena usually need new training frames.

## Analysis

**A video is skipped ("missing predictions" or "missing bowl").**
Each video needs both. The video list's **P** and **B** badges, and the *Status* filter, show which videos are
missing what.

**No bouts are found.**
Check in the Tracking tab:

- whether the dark-blue "in bowl" marks appear when the animal feeds. If not, the rim may be too small, or
  `bouts.node` is the wrong keypoint.
- whether the frames around the bowl are tracked (light blue). Feeding often hides keypoints; if those frames are
  grey, lower `quality.min_score` or drop the hidden keypoint (`quality.drop_nodes`).
- whether visits are long enough (`bouts.min_contact_frames`), and whether the approach and withdrawal are tracked for
  `bouts.flank_frames` frames.

**Too many short bouts, or bouts merged together.**
Tune `bouts.merge_gap_frames`: smaller values split visits more readily. Tune `bouts.min_contact_frames`: larger
values ignore brief touches.

**"No comparisons" in the run notes.**
The standard comparison needs at least two groups (*Settings → Animals and groups*) and two conditions. Or define your
own comparison in *Results → Comparisons*.

**A comparison's group shows 0 animals.**
No analysed video matches the group's values. Check the ticked values, and whether the videos are ready (predictions
and bowl).

**Paired tests where I expected independent ones (or the reverse).**
Set the comparison's *Paired* option explicitly (*Always* or *Never*). See
[Paired and independent tests](10-comparisons.md#paired-and-independent-tests).

## Data and projects

**Can I move a project to another computer?**
Yes. Copy the project folder. Paths inside it are relative. Videos used in place from elsewhere must be at the same
path, or be re-added in *Settings → Videos*. Models outside the project must be re-assigned (or copied in with *Copy
in* beforehand).

**Can several people annotate the same project?**
Bowl annotations are one small file per video, so people can split the videos, then combine their
`annotations/bowls/` files (e.g. with git).

**Where are my projects?**
By default in `projects/` inside the app folder; the start page lists recent projects. The top bar shows the open
project's name; hover over it for the path.

**How do I cite the methods?**
The [Statistical methods](11-statistics.md) and [Running the analysis](08-analysis.md) chapters give the definitions.
Report the code version (commit) and the settings from the run's `manifest.json` and `feeding.yaml`.

## Getting help

Open an issue on the repository with:

- what you did and what happened;
- the output of `feeding check`;
- for failed jobs, the log from the Jobs drawer (also saved in `data/logs/`);
- for unexpected results, the run's `manifest.json`.
