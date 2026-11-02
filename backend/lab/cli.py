"""The lab's commands. From the repository root: `make lab ARGS="<command> ..."`."""

from pathlib import Path

import typer

from lab.compare import format_comparison
from lab.diagnose import run_diagnosis
from lab.experiment import ExperimentError, load_experiment
from lab.importer import LabImportError, import_study
from lab.layout import EXPERIMENTS_DIR, REPO_ROOT, lab_data_root, sessions_root
from lab.run import load_scores, run_experiment, score_reference

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _experiment_file(name_or_path: str) -> Path:
    path = Path(name_or_path)
    if path.suffix == ".toml" and path.is_file():
        return path
    candidate = EXPERIMENTS_DIR / f"{name_or_path}.toml"
    if candidate.is_file():
        return candidate
    known = ", ".join(sorted(p.stem for p in EXPERIMENTS_DIR.glob("*.toml")))
    raise typer.BadParameter(f"no experiment '{name_or_path}' (known: {known})")


@app.command("import-study")
def import_study_cmd(
    study: int = typer.Argument(..., help="Study id."),
    source: Path = typer.Option(REPO_ROOT / "data", help="Data root to copy from (only read)."),
    force: bool = typer.Option(False, help="Replace sessions already in the lab."),
    assume_dot: str = typer.Option(
        "", help="Score every played video against this dot video (test sessions only)."
    ),
) -> None:
    """Copy a study's processed sessions into the lab; records which dot videos each played."""
    try:
        results = import_study(source, sessions_root(lab_data_root()), study, force,
                               assume_dot or None)  # fmt: skip
    except LabImportError as e:
        raise typer.BadParameter(str(e)) from e
    for r in results:
        typer.echo(f"{r.session.study_id} {r.session.code:8} {r.session.session_id}  {r.status}")


@app.command("reference")
def reference_cmd(studies: list[int] = typer.Argument(..., help="Study ids.")) -> None:
    """Score the product pipeline's own outputs (as copied from data/) as `reference`."""
    result, report = score_reference(lab_data_root(), tuple(studies))
    typer.echo(report)


@app.command("run")
def run_cmd(experiment: str = typer.Argument(..., help="Experiment name or .toml file.")) -> None:
    """Run an experiment on its studies and print its scores."""
    try:
        exp = load_experiment(_experiment_file(experiment))
    except ExperimentError as e:
        raise typer.BadParameter(str(e)) from e
    result, report = run_experiment(exp, lab_data_root())
    typer.echo(report)
    if result.failures:
        raise typer.Exit(1)


@app.command("compare")
def compare_cmd(
    new: str = typer.Argument(..., help="Experiment to judge."),
    base: str = typer.Option("baseline", help="Experiment to compare with."),
    only_video: str = typer.Option("", help="Compare only the rows of this dot video."),
) -> None:
    """Session-by-session change against the base, and the acceptance verdict."""
    lab = lab_data_root()
    new_head, new_scores = load_scores(lab, new)
    _, base_scores = load_scores(lab, base)
    if only_video:
        new_scores = [s for s in new_scores if s.video == only_video]
        base_scores = [s for s in base_scores if s.video == only_video]
    tune = new_head.get("tune_studies", [])
    holdout = new_head.get("holdout_studies", [])
    typer.echo(format_comparison(base, base_scores, new, new_scores, tune, holdout))


@app.command("diagnose")
def diagnose_cmd(
    experiment: str = typer.Argument("baseline", help="Experiment whose outputs to diagnose."),
    sweep: bool = typer.Option(True, help="Also re-run the pipeline with each ridge alpha alone."),
) -> None:
    """E1: head pose against calibration, what moves the gaze, timing, regularisation."""
    _, report = run_diagnosis(lab_data_root(), experiment, sweep)
    typer.echo(report)


@app.command("plot")
def plot_cmd(experiment: str = typer.Argument(..., help="Experiment whose gaze to plot.")) -> None:
    """x and y of the dot and the gaze over each dot video's time, one PNG per session and video."""
    from lab.plots import plot_experiment

    for path in plot_experiment(lab_data_root(), experiment):
        typer.echo(path)


@app.command("replay")
def replay_cmd(
    study: int = typer.Argument(..., help="Study id."),
    code: str = typer.Argument(..., help="Participant code of the session."),
    experiments: list[str] = typer.Argument(..., help="Experiments to show (one dot each)."),
    video: str = typer.Option("moving_dot", help="Dot video: moving_dot, long_dot_1, long_dot_2."),
) -> None:
    """A film of the dot video with the gaze of each experiment, re-run on this one session."""
    from lab.replay import render_replay
    from worker.pipeline.replay import ReplayError

    last = ""

    def progress(message: str, _share: float) -> None:
        nonlocal last
        if message != last:
            typer.echo(message)
            last = message

    try:
        exps = [load_experiment(_experiment_file(e)) for e in experiments]
        path, failures = render_replay(lab_data_root(), study, code, video, exps, progress)
    except (ExperimentError, ReplayError) as e:
        raise typer.BadParameter(str(e)) from e
    for f in failures:
        typer.echo(f"left out: {f.pipeline}: {f.reason}")
    typer.echo(path)


@app.command("ui")
def ui_cmd(
    port: int = typer.Option(8765, help="Port on 127.0.0.1."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the page."),
) -> None:
    """A local page with buttons for `replay` (the films contain webcam video: localhost only)."""
    import threading
    import webbrowser

    import uvicorn

    from lab.ui import HOST, create_app

    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(f"http://{HOST}:{port}/",)).start()
    uvicorn.run(create_app(lab_data_root()), host=HOST, port=port, log_level="warning")


@app.command("extract-blendshapes")
def extract_blendshapes_cmd(
    studies: list[int] = typer.Argument(..., help="Study ids already in the lab."),
    force: bool = typer.Option(False, help="Extract again even when the file exists."),
) -> None:
    """E8: run MediaPipe once per session and cache every blendshape score and the iris depth."""
    from app.storage import derived_dir, raw_dir
    from lab.blendshapes import BLENDSHAPES_FILE, extract, model_file
    from lab.layout import list_sessions

    lab = lab_data_root()
    model = model_file(lab / "models")
    for study in studies:
        for ref in list_sessions(sessions_root(lab), study):
            out = derived_dir(sessions_root(lab), study, ref.session_id) / BLENDSHAPES_FILE
            if out.exists() and not force:
                typer.echo(f"{study} {ref.code:8} exists")
                continue
            n = extract(model, raw_dir(sessions_root(lab), study, ref.session_id) / "webcam.webm",
                        out)  # fmt: skip
            typer.echo(f"{study} {ref.code:8} {n} frames")


def main() -> None:
    app()
