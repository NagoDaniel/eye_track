"""A small local page to render lab replay films with buttons (`make lab ARGS="ui"`).

Choose a session and a dot video, tick experiments (the files of `lab/experiments/` or custom
ones made on the page), press the button, watch the film. Listens on 127.0.0.1 only: the films
contain the participant's webcam. One film is rendered at a time.
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from lab.experiment import Experiment, ExperimentError, load_experiment, parse_experiment
from lab.layout import EXPERIMENTS_DIR, list_sessions, read_labels, sessions_root
from lab.models import MODELS
from lab.replay import REPLAYS_DIR, dot_videos, render_replay
from lab.variants import FIT_VALIDATION
from worker.pipeline.replay import ReplayError

PAGE = (Path(__file__).parent / "ui_page.html").read_text(encoding="utf-8")
HOST = "127.0.0.1"
PORT = 8765
FIRST_EXPERIMENTS = ("baseline", "pre-t14", "t-ridge", "t-krr", "t-mlp", "t-svr", "t-gbr", "t-gp",
                     "t-tps")  # fmt: skip


class CustomVariant(BaseModel):
    model: str = "product"
    fit_validation: str = "all"
    window_ms: float | None = None
    blink_relative_delta: float | None = None


class ReplayRequest(BaseModel):
    study: int
    code: str
    video: str
    experiments: list[str] = []
    custom: list[CustomVariant] = []


@dataclass
class Job:
    state: str = "idle"  # idle, running, done, error
    message: str = ""
    share: float = 0.0
    film: str | None = None
    notes: list[str] = field(default_factory=list)


def custom_experiment(c: CustomVariant) -> Experiment:
    """An experiment from the page's form (the study id only satisfies the format)."""
    parts = [c.model, f"fv-{c.fit_validation}"]
    data: dict[str, Any] = {"pipelines": ["a"], "tune_studies": [0], "model": c.model,
                            "fit_validation": c.fit_validation}  # fmt: skip
    if c.window_ms is not None:
        data["config"] = {"calib_window_before_click_ms": c.window_ms}
        parts.append(f"w{c.window_ms:g}")
    if c.blink_relative_delta is not None:
        data["blink_relative_delta"] = c.blink_relative_delta
        parts.append(f"rel{c.blink_relative_delta:g}")
    data["name"] = "custom-" + "-".join(parts).replace(".", "p")
    data["description"] = "made on the page"
    return parse_experiment(data)


def experiment_files() -> dict[str, Path]:
    return {p.stem: p for p in sorted(EXPERIMENTS_DIR.glob("*.toml"))}


def describe_experiments() -> list[dict[str, str]]:
    out = []
    files = experiment_files()
    names = [
        *(n for n in FIRST_EXPERIMENTS if n in files),
        *(n for n in files if n not in FIRST_EXPERIMENTS),
    ]
    for name in names:
        try:
            exp = load_experiment(files[name])
        except ExperimentError:
            continue
        if "a" in exp.pipelines:
            out.append({"name": name, "model": exp.variant.model, "description": exp.description})
    return out


def create_app(lab_data: Path) -> FastAPI:
    app = FastAPI(title="Lab replay", docs_url=None, redoc_url=None, openapi_url=None)
    job = Job()
    lock = threading.Lock()
    films = lab_data / REPLAYS_DIR

    @app.get("/", response_class=HTMLResponse)
    def page() -> str:
        return PAGE

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        labels = read_labels(lab_data)
        sessions = []
        studies = sorted(
            int(p.name) for p in (sessions_root(lab_data) / "studies").glob("*") if p.name.isdigit()
        )[::-1]  # newest study first
        for study in studies:
            for ref in list_sessions(sessions_root(lab_data), study):
                try:
                    videos = [d.name for d in dot_videos(lab_data, ref)]
                except FileNotFoundError:
                    continue
                sessions.append({"study": study, "code": ref.code, "videos": videos,
                                 "label": labels.get((study, ref.code), "")})  # fmt: skip
        return {
            "sessions": sessions,
            "experiments": describe_experiments(),
            "models": list(MODELS),
            "fit_validation": list(FIT_VALIDATION),
            "films": sorted((f.name for f in films.glob("*.mp4")), reverse=True)
            if films.is_dir() else [],
        }  # fmt: skip

    def work(request: ReplayRequest, experiments: list[Experiment]) -> None:
        def progress(message: str, share: float) -> None:
            job.message, job.share = message, share

        try:
            path, failures = render_replay(lab_data, request.study, request.code, request.video,
                                           experiments, progress)  # fmt: skip
            job.film = path.name
            job.notes = [f"{f.pipeline}: {f.reason}" for f in failures]
            job.state = "done"
        except (ReplayError, FileNotFoundError, ValueError) as e:
            job.state, job.message = "error", str(e)
        except Exception as e:  # shown on the page; the lab is a developer tool
            job.state, job.message = "error", f"{type(e).__name__}: {e}"

    @app.post("/api/replay")
    def start(request: ReplayRequest) -> dict[str, str]:
        files = experiment_files()
        try:
            experiments = [load_experiment(files[n]) for n in request.experiments]
            experiments += [custom_experiment(c) for c in request.custom]
        except KeyError as e:
            raise HTTPException(404, f"no experiment {e}") from e
        except ExperimentError as e:
            raise HTTPException(422, str(e)) from e
        with lock:
            if job.state == "running":
                raise HTTPException(409, "a film is being rendered")
            job.state, job.message, job.share, job.film, job.notes = (
                "running",
                "starting",
                0.0,
                None,
                [],
            )
        threading.Thread(target=work, args=(request, experiments), daemon=True).start()
        return {"state": "running"}

    @app.get("/api/job")
    def status() -> dict[str, Any]:
        return {"state": job.state, "message": job.message, "share": job.share, "film": job.film,
                "notes": job.notes}  # fmt: skip

    @app.get("/films/{name}")
    def film(name: str) -> FileResponse:
        path = films / name
        if "/" in name or name.startswith(".") or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")

    return app
