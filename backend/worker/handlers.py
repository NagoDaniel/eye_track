"""Job handlers of the worker (PLAN §6, §8): `process_session` runs the pipeline for one session."""

import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.aois import parse_aoi_file
from app.chunks import MIB
from app.models import Chunk, Job, SessionStatus, Study, Video
from app.models import Session as SessionRow
from app.settings import Settings
from app.storage import derived_dir
from worker.analysis.analyze import VideoInfo, analyze, load_included
from worker.analysis.config import AnalysisConfig
from worker.analysis.run import start_run
from worker.analysis.select import query_sessions, select_sessions
from worker.pipeline import runner
from worker.pipeline.config import PipelineConfig
from worker.pipeline.ingest import ChunkRecord
from worker.pipeline.quality import QUALITY_FILE
from worker.pipeline.replay import render_replay
from worker.pipeline.runner import Context, is_pipeline_flag, pipeline_flags

log = logging.getLogger("worker")

# A session can be processed again from any of these states (re-runs, PLAN §12.2).
PROCESSABLE = (
    SessionStatus.UPLOADED,
    SessionStatus.PROCESSING,  # left behind by a worker that died
    SessionStatus.PROCESSED,
    SessionStatus.FAILED,
)


def _manifest(db: Session, session_id: uuid.UUID) -> list[ChunkRecord]:
    rows = db.scalars(select(Chunk).where(Chunk.session_id == session_id)).all()
    return [ChunkRecord(r.stream.value, r.seq, r.size, r.sha256) for r in rows]


def make_process_session(
    factory: sessionmaker[Session], settings: Settings, config: PipelineConfig | None = None
) -> Callable[[Job], str]:
    """The handler for `process_session` jobs. Args: `session_id`, optionally `from_stage` or
    `only` (a stage name), `keep_intermediates`. The session is `processing` while it runs, then
    `processed` (the quality report exists) or `failed`; a stage that raises fails the job, with the
    stage in its log."""
    pipeline_config = config or PipelineConfig()

    def handle(job: Job) -> str:
        raw = job.args.get("session_id")
        if not isinstance(raw, str):
            raise ValueError("process_session needs a session_id in its args")
        session_id = uuid.UUID(raw)
        from_stage, only = job.args.get("from_stage"), job.args.get("only")

        with factory() as db:
            session = db.get(SessionRow, session_id)
            if session is None:
                raise LookupError(f"session {session_id} does not exist")
            if session.status not in PROCESSABLE:
                raise ValueError(f"session {session_id} is {session.status.value}, not complete")
            study_id, previous = session.study_id, session.status
            manifest = _manifest(db, session_id)
            session.status = SessionStatus.PROCESSING
            db.commit()

        ctx = Context(
            root=settings.data_root,
            study_id=study_id,
            session_id=session_id,
            config=pipeline_config,
            model=settings.face_landmarker_model,
            max_inflated_bytes=settings.max_events_chunk_inflated_mb * MIB,
            manifest=manifest,
            keep_intermediates=bool(job.args.get("keep_intermediates")),
        )
        try:
            stages = runner.run_pipeline(ctx, from_stage=from_stage, only=only)
        except BaseException:  # a stage that raised (StageFailed), or the worker being stopped
            _finish(factory, session_id, SessionStatus.FAILED, None, [])
            raise
        derived = derived_dir(settings.data_root, study_id, session_id)
        report = _read_report(derived / QUALITY_FILE)
        # A run of early stages on a session never processed before has no report yet.
        status = SessionStatus.PROCESSED if report is not None else previous
        _finish(factory, session_id, status, report, pipeline_flags(derived))
        return f"processed: {', '.join(stages)}"

    return handle


def _read_report(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _finish(
    factory: sessionmaker[Session],
    session_id: uuid.UUID,
    status: SessionStatus,
    report: dict[str, Any] | None,
    flags: list[str],
) -> None:
    with factory() as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            return
        session.status = status
        if report is not None:
            session.quality = report
            kept = [f for f in session.flags if not is_pipeline_flag(f)]
            session.flags = kept + [f for f in flags if f not in kept]
        db.commit()


def make_render_debug(
    factory: sessionmaker[Session], settings: Settings, config: PipelineConfig | None = None
) -> Callable[[Job], str]:
    """The handler for `render_debug` jobs (PLAN §12.2): draw the debug replay of a processed
    session. Args: `session_id`. The session's status is not touched; a failure fails the job."""
    pipeline_config = config or PipelineConfig()

    def handle(job: Job) -> str:
        raw = job.args.get("session_id")
        if not isinstance(raw, str):
            raise ValueError("render_debug needs a session_id in its args")
        session_id = uuid.UUID(raw)
        with factory() as db:
            session = db.get(SessionRow, session_id)
            if session is None:
                raise LookupError(f"session {session_id} does not exist")
            study_id = session.study_id
        summary = render_replay(settings.data_root, study_id, session_id, pipeline_config)
        return f"replay rendered: {summary.frames} frames, {summary.bytes} bytes"

    return handle


def make_analyze_video(factory: sessionmaker[Session], settings: Settings) -> Callable[[Job], str]:
    """The handler for `analyze_video` jobs (PLAN §9). Args: `study_id`, `video_id`, `run_id`,
    optionally `gaze_columns` and `aoi_tolerance_k`. Writes `analysis/{study}/{video}/{run}/` with
    `run.json`; a run that cannot produce anything (or whose stage raises) is marked `failed` there
    and fails the job."""

    def handle(job: Job) -> str:
        study_id, video_id, run_id = (job.args.get(k) for k in ("study_id", "video_id", "run_id"))
        if not (
            isinstance(study_id, int) and isinstance(video_id, int) and isinstance(run_id, str)
        ):
            raise ValueError("analyze_video needs study_id, video_id and run_id in its args")
        config = AnalysisConfig(
            gaze_columns=job.args.get("gaze_columns", "plain"),
            aoi_tolerance_k=float(job.args.get("aoi_tolerance_k", 0.0)),
        )
        with factory() as db:
            if db.get(Study, study_id) is None:
                raise LookupError(f"study {study_id} does not exist")
            video = db.get(Video, video_id)
            if video is None:
                raise LookupError(f"video {video_id} does not exist")
            info = VideoInfo(video.id, video.duration, video.width, video.height)
            aois = parse_aoi_file(video.aois or {})
            infos = query_sessions(db, settings.data_root, study_id)

        selection = select_sessions(infos, video_id)
        record, run_dir = start_run(
            settings.data_root, study_id, video_id, run_id, config, selection
        )
        try:
            sessions = load_included(
                settings.data_root, study_id, video_id, selection, config, record
            )
            analyze(settings.data_root, run_dir, record, info, aois, sessions)
        except (
            BaseException
        ) as exc:  # an AnalysisError, a stage that raised, or the worker stopping
            record.finish(run_dir, error=str(exc) or type(exc).__name__)
            raise
        record.finish(run_dir)
        return f"analysed {len(sessions)} sessions: {', '.join(record.outputs)}"

    return handle
