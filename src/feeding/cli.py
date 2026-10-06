"""Feeding-behaviour analysis. Commands act on a project folder (one containing
feeding.yaml): --project DIR, or the current folder / a parent folder."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from .config import NoProjectError, load_project

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
ProjectOpt = Annotated[Optional[Path], typer.Option(
    "--project", "-p", "--config", "-c",
    help="Project folder (or its feeding.yaml). Default: the current folder or a parent containing feeding.yaml")]


def _project(path: Path | None):
    try:
        return load_project(path)
    except NoProjectError as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Project folder to create (may already exist)")],
    model: Annotated[Optional[list[str]], typer.Option(
        help="SLEAP model folder: PATH for all videos, or CONTEXT=PATH per context; repeat for several "
             "(same key twice = top-down pair). Optional: can be set later with `feeding set-model`")] = None,
    videos: Annotated[Optional[list[Path]], typer.Option(
        help="Folder(s) of per-animal videos to use in place (read-only). Videos can also be "
             "copied into DIRECTORY/videos, or added later with `feeding add-videos`")] = None,
    sleap_bin: Annotated[Optional[Path], typer.Option(
        help="bin/ of the SLEAP environment (default: found automatically when needed)")] = None,
    pattern: Annotated[Optional[str], typer.Option(
        help="Regex for video file names with a named group (?P<animal>...); optional groups: "
             "session, part, condition, context. Default: guessed from the video names")] = None,
    chamber: Annotated[Optional[list[str]], typer.Option(help="CONTEXT=LABEL display names, e.g. A=square")] = None,
    name: Annotated[Optional[str], typer.Option(help="Project name (default: folder name)")] = None,
    force: bool = False,
):
    """Create a project folder. Only the folder is required; everything else can be added later."""
    from .project import check_project, init_project, parse_model_args

    chambers = dict(c.split("=", 1) for c in chamber or [])
    cfg_path, notes = init_project(directory, parse_model_args(model or []), videos, sleap_bin,
                                   pattern, chambers, name, force=force)
    cfg = load_project(cfg_path)
    typer.echo(f"Created project {cfg.project_name} at {cfg.root}")
    typer.echo(f"  naming pattern {cfg.naming.pattern}")
    for k, v in cfg.sleap.models.items():
        typer.echo(f"  model[{k}]: {v}")
    for n in notes:
        typer.echo(f"  note: {n}")
    for i in check_project(cfg):
        typer.echo(f"  {i['level']}: {i['message']}")
    typer.echo(f"\nOpen it with: feeding serve {cfg.root}")


@app.command("add-videos")
def add_videos_cmd(
    files: Annotated[list[Path], typer.Argument(help="Video files to copy into the project, or folders to use in place")],
    move: Annotated[bool, typer.Option(help="Move files instead of copying")] = False,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project folder")] = None,
):
    """Add videos: files are copied into <project>/videos; folders are referenced in place."""
    from .project import add_video_folder, add_videos

    cfg = _project(project)
    folders = [f for f in files if f.is_dir()]
    vids = [f for f in files if f.is_file()]
    for f in folders:
        cfg = add_video_folder(cfg, f)
        typer.echo(f"using folder {f.resolve()}")
    if vids:
        cfg, added = add_videos(cfg, vids, move=move)
        typer.echo(f"added {len(added)} videos to {cfg.root / 'videos'}")


@app.command("set-model")
def set_model_cmd(
    model: Annotated[list[str], typer.Argument(help="PATH (all videos) or CONTEXT=PATH; repeat for several")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project folder")] = None,
):
    """Assign SLEAP model(s) to the project (replaces the current assignment)."""
    from .project import parse_model_args, set_models

    cfg, notes = set_models(_project(project), parse_model_args(model))
    for k, v in cfg.sleap.models.items():
        typer.echo(f"model[{k}]: {v}")
    for n in notes:
        typer.echo(f"note: {n}")


@app.command("models")
def models_cmd(project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project folder")] = None):
    """List SLEAP models in the model library (and the project's models/ folder)."""
    from . import settings as app_settings

    folders = app_settings.model_library()
    try:
        folders.append(load_project(project).root / "models")
    except NoProjectError:
        pass
    for m in app_settings.find_models(folders):
        typer.echo(f"{m['name']}\n    {m['path']}\n    nodes {m['nodes']}  heads {m['heads']}")


@app.command("config")
def config_cmd(
    key: Annotated[Optional[str], typer.Argument(help="Dotted setting, e.g. sleap.batch_size; omit to show all")] = None,
    value: Annotated[Optional[str], typer.Argument(help="New value (YAML: 8, true, [a, b], null to remove)")] = None,
    project: ProjectOpt = None,
):
    """Show or change a project setting in feeding.yaml (comments are kept; the result is validated).

    Examples: `feeding config sleap`, `feeding config sleap.peak_threshold 0.3`,
    `feeding config analysis.conditions "[Pre, Post]"`.
    """
    import yaml

    from .project import update_project

    cfg = _project(project)
    if value is not None:
        try:
            cfg = update_project(cfg, {key: yaml.safe_load(value)})
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    data = cfg.model_dump(mode="json")
    for k in (key or "").split(".") if key else []:
        if not isinstance(data, dict) or k not in data:
            raise typer.BadParameter(f"no setting {key!r}")
        data = data[k]
    typer.echo(yaml.safe_dump(data, sort_keys=False, default_flow_style=None).rstrip()
               if isinstance(data, (dict, list)) else str(data))


@app.command()
def check(project: ProjectOpt = None):
    """Check a project for problems (missing videos/models, unmapped contexts, subjects)."""
    from .project import check_project

    cfg = _project(project)
    issues = check_project(cfg)
    typer.echo(f"Project {cfg.project_name} ({cfg.root})")
    for i in issues:
        typer.echo(f"  {i['level']}: {i['message']}")
    if not issues:
        typer.echo("  no problems found")
    raise typer.Exit(1 if any(i["level"] == "error" for i in issues) else 0)


@app.command()
def status(project: ProjectOpt = None):
    """List videos with prediction / bowl-annotation status."""
    from .pipeline import video_status

    cfg = _project(project)
    df = video_status(cfg)
    typer.echo(df[["video", "condition", "context", "animal", "group", "has_predictions", "has_bowl"]].to_string(index=False))
    typer.echo(f"\n{len(df)} videos | predictions: {df.has_predictions.sum()} | bowls: {df.has_bowl.sum()} "
               f"| ready: {(df.has_predictions & df.has_bowl).sum()}")


@app.command()
def infer(
    videos: Annotated[Optional[list[str]], typer.Argument(help="Video names (stems); omit with --all")] = None,
    all_: Annotated[bool, typer.Option("--all", help="All videos without predictions")] = False,
    force: Annotated[bool, typer.Option(help="Re-run even if predictions exist")] = False,
    batch_size: Annotated[Optional[int], typer.Option(help="Frames per batch (sleap.batch_size)")] = None,
    peak_threshold: Annotated[Optional[float], typer.Option(help="Minimum keypoint confidence, 0-1 (sleap.peak_threshold)")] = None,
    device: Annotated[Optional[str], typer.Option(help="auto | cpu | gpu | GPU index (sleap.device)")] = None,
    tracker: Annotated[Optional[str], typer.Option(help="none | simple | flow (sleap.tracker)")] = None,
    target_instance_count: Annotated[Optional[int], typer.Option(help="Animals per frame for the tracker; 0 = no limit")] = None,
    sleap_arg: Annotated[Optional[list[str]], typer.Option(
        "--sleap-arg", help="Extra sleap-track argument, passed verbatim (repeat; e.g. --sleap-arg=--frames --sleap-arg=0-99)")] = None,
    save: Annotated[bool, typer.Option(help="Also save these options to feeding.yaml")] = False,
    project: ProjectOpt = None,
):
    """Run SLEAP inference on one or more videos.

    Options default to the project's `sleap:` settings; given here they apply to this run (and with --save, to later runs too). Each video's .provenance.json records them.
    """
    from .pipeline import video_status
    from .sleap_runner import run_inference

    cfg = _project(project)
    given = {"batch_size": batch_size, "peak_threshold": peak_threshold, "device": device, "tracker": tracker,
             "target_instance_count": target_instance_count, "extra_args": sleap_arg}
    given = {k: v for k, v in given.items() if v is not None}
    if given:
        try:
            if save:
                from .project import update_project

                cfg = update_project(cfg, {f"sleap.{k}": v for k, v in given.items()})
            else:
                cfg = cfg.model_copy(update={"sleap": cfg.sleap.model_validate({**cfg.sleap.model_dump(), **given})})
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    typer.echo("sleap-track " + " ".join(cfg.sleap.track_args()))
    st = video_status(cfg)
    if all_:
        todo = st if force else st[~st.has_predictions]
    elif videos:
        todo = st[st.video.isin(videos)]
        if not force:
            todo = todo[~todo.has_predictions]
    else:
        raise typer.BadParameter("give video names or --all")
    for i, row in enumerate(todo.itertuples(index=False), 1):
        typer.echo(f"[{i}/{len(todo)}] {row.video}")

        def progress(m, _v=row.video):
            typer.echo(f"\r  {m.get('n_processed')}/{m.get('n_total')} frames, eta {m.get('eta', 0):.0f}s   ", nl=False)

        run_inference(cfg, Path(row.path), on_progress=progress, on_log=lambda s: None)
        typer.echo()


@app.command("import-predictions")
def import_predictions(
    source_dirs: Annotated[list[Path], typer.Argument(help="Directories containing SLEAP analysis .h5 files")],
    overwrite: bool = False,
    project: ProjectOpt = None,
):
    """Copy existing SLEAP analysis .h5 files (named {video}.h5) into the predictions folder."""
    from .pipeline import video_status
    from .sleap_runner import has_predictions, import_analysis_h5

    cfg = _project(project)
    paths = {r.video: Path(r.path) for r in video_status(cfg).itertuples()}
    n = 0
    for d in source_dirs:
        for src in sorted(Path(d).glob("*.h5")):
            name = src.name.removesuffix(".analysis.h5").removesuffix(".h5")
            if name not in paths:
                typer.echo(f"skip {src.name}: no matching video")
                continue
            if has_predictions(cfg, name) and not overwrite:
                continue
            import_analysis_h5(cfg, src, paths[name], cfg.skeleton.nodes)
            n += 1
    typer.echo(f"imported {n} files")


@app.command()
def split(overwrite: bool = False, project: ProjectOpt = None):
    """Optional: split recordings of four arenas (2x2 grid) into one video per animal.

    Files in split.raw_videos named {prefix}_{TL}_{TR}_{BL}_{BR} (animal IDs of the top-left, top-right,
    bottom-left and bottom-right arenas) become {prefix}_{animal}.mp4 in split.output.
    """
    from .naming import video_files
    from .video import split_quadrants

    cfg = _project(project)
    if cfg.split.raw_videos is None:
        raise typer.BadParameter("split.raw_videos is not set in the config")
    raws = video_files(cfg.split.raw_videos)
    for i, raw in enumerate(raws, 1):
        typer.echo(f"[{i}/{len(raws)}] {raw.name}")
        split_quadrants(raw, cfg.split.output, cfg.split.crf, cfg.split.gop, overwrite)


@app.command()
def analyze(
    videos: Annotated[Optional[list[str]], typer.Argument(help="Restrict to these videos")] = None,
    plots: Annotated[bool, typer.Option(help="Render figures")] = True,
    project: ProjectOpt = None,
):
    """Bouts, features, statistics and figures for every ready video."""
    from .pipeline import run_analysis

    run_analysis(_project(project), videos or None, make_plots=plots, log=typer.echo)


@app.command()
def compare(
    run: Annotated[Optional[str], typer.Argument(help="Analysis run folder name (default: the latest run)")] = None,
    view: Annotated[Optional[list[str]], typer.Option("--view", "-v", help="Only these comparisons (name or slug; repeatable)")] = None,
    plots: Annotated[bool, typer.Option(help="Render figures")] = True,
    project: ProjectOpt = None,
):
    """Run the project's comparison views on an existing analysis run (no video re-processing)."""
    from .views import load_run_tables, run_views, views_for

    cfg = _project(project)
    runs = sorted(d for d in cfg.paths.results.glob("*/") if (d / "manifest.json").exists()) if cfg.paths.results.exists() else []
    if not runs:
        raise typer.BadParameter("no analysis runs yet: run `feeding analyze` first")
    run_dir = cfg.paths.results / run if run else runs[-1]
    if not (run_dir / "manifest.json").exists():
        raise typer.BadParameter(f"{run_dir} is not an analysis run")
    views = views_for(cfg, load_run_tables(run_dir)[1])
    if view:
        views = [v for v in views if v.name in view or v.slug in view]
        if not views:
            raise typer.BadParameter("no comparison with that name; see `views:` in feeding.yaml")
    for s in run_views(run_dir, cfg, views, make_plots=plots, log=typer.echo):
        msg = f"error: {s['error']}" if s.get("error") else (
            f"{s['n_sig_bouts']} bout + {s['n_sig_sessions']} session results with adjusted p < 0.05")
        typer.echo(f"  {s['name']}: {msg}  -> {run_dir / 'views' / s['slug']}")


@app.command()
def embed(
    train: Annotated[bool, typer.Option(help="Train a new model on bowl-centred features instead of using the trained checkpoint")] = False,
    epochs: Annotated[int, typer.Option(help="Training epochs (with --train)")] = 50,
    project: ProjectOpt = None,
):
    """LSTM-VAE embedding + k-means behaviour clustering (needs the 'embed' extra).

    Uses the trained model in embedding.checkpoint, or trains a new one with --train.
    """
    from .embedding import run_embedding

    run_embedding(_project(project), train=train, epochs=epochs, log=typer.echo)


@app.command()
def demo(
    directory: Annotated[Optional[Path], typer.Argument(help="Folder to create (default: <projects folder>/demo)")] = None,
    animals: Annotated[int, typer.Option(help="Animals per group (two groups)")] = 5,
    seconds: Annotated[float, typer.Option(help="Length of each video (seconds)")] = 120.0,
    seed: int = 0,
    bowls: Annotated[bool, typer.Option(help="Annotate the bowls (--no-bowls leaves that to you)")] = True,
    force: Annotated[bool, typer.Option(help="Recreate an existing demo project")] = False,
):
    """Create a demo project with synthetic videos and SLEAP-format predictions (no SLEAP needed)."""
    from . import settings as app_settings
    from .demo import make_demo

    root = directory or app_settings.projects_dir() / "demo"
    try:
        cfg_path = make_demo(root, animals, seconds, seed, bowls, force, log=lambda s: None,
                             progress=lambda i, n: typer.echo(f"\r  rendering video {i}/{n}", nl=False))
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc)) from None
    typer.echo(f"\nCreated the demo project at {cfg_path.parent}\nOpen it with: feeding serve {cfg_path.parent}")


@app.command()
def serve(
    project_dir: Annotated[Optional[Path], typer.Argument(
        help="Project folder to open (default: the current folder's project, if any; "
             "otherwise pick one in the UI)")] = None,
    host: Annotated[str, typer.Option(help="127.0.0.1 = this machine only")] = "127.0.0.1",
    port: int = 8765,
    project: ProjectOpt = None,
):
    """Start the browser UI."""
    import os

    import uvicorn

    target = project_dir or project
    if target:
        os.environ["FEEDING_PROJECT"] = str(_project(target).source)
    typer.echo(f"Feeding UI: http://{host}:{port}")
    uvicorn.run("feeding.server.app:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
