"""The lab's commands. From the repository root: `make lab ARGS="<command> ..."`."""

from pathlib import Path

import typer

from lab.compare import format_comparison
from lab.experiment import ExperimentError, load_experiment
from lab.importer import LabImportError, import_study
from lab.layout import DOT_PATH_FILE, EXPERIMENTS_DIR, REPO_ROOT, lab_data_root, sessions_root
from lab.run import load_scores, run_experiment, score_reference
from lab.score import DotTruth

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
) -> None:
    """Copy a study's processed sessions into the lab."""
    try:
        results = import_study(source, sessions_root(lab_data_root()), study, force)
    except LabImportError as e:
        raise typer.BadParameter(str(e)) from e
    for r in results:
        typer.echo(f"{r.session.study_id} {r.session.code:8} {r.session.session_id}  {r.status}")


@app.command("reference")
def reference_cmd(studies: list[int] = typer.Argument(..., help="Study ids.")) -> None:
    """Score the product pipeline's own outputs (as copied from data/) as `reference`."""
    result, report = score_reference(lab_data_root(), tuple(studies), DotTruth.load(DOT_PATH_FILE))
    typer.echo(report)


@app.command("run")
def run_cmd(experiment: str = typer.Argument(..., help="Experiment name or .toml file.")) -> None:
    """Run an experiment on its studies and print its scores."""
    try:
        exp = load_experiment(_experiment_file(experiment))
    except ExperimentError as e:
        raise typer.BadParameter(str(e)) from e
    result, report = run_experiment(exp, lab_data_root(), DotTruth.load(DOT_PATH_FILE))
    typer.echo(report)
    if result.failures:
        raise typer.Exit(1)


@app.command("compare")
def compare_cmd(
    new: str = typer.Argument(..., help="Experiment to judge."),
    base: str = typer.Option("baseline", help="Experiment to compare with."),
) -> None:
    """Session-by-session change against the base, and the acceptance verdict."""
    lab = lab_data_root()
    new_head, new_scores = load_scores(lab, new)
    _, base_scores = load_scores(lab, base)
    tune = new_head.get("tune_studies", [])
    holdout = new_head.get("holdout_studies", [])
    typer.echo(format_comparison(base, base_scores, new, new_scores, tune, holdout))


def main() -> None:
    app()
