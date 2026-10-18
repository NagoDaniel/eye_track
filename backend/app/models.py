"""SQLAlchemy models (PLAN §4). Enum values are stored as plain strings with a CHECK constraint."""

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

Json = JSON().with_variant(JSONB(), "postgresql")


def now() -> datetime:
    return datetime.now(UTC)


def str_enum(cls: type[enum.Enum], name: str) -> Enum:
    # NOTE: alembic autogenerate duplicates the CHECK as explicit CheckConstraint lines;
    # delete them from generated migrations (see docs/PROGRESS.md, open problems).
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


class StudyMode(enum.Enum):
    REMOTE = "remote"
    LAB = "lab"


class SessionStatus(enum.Enum):
    CREATED = "created"
    CONSENTED = "consented"
    RECORDING = "recording"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


class ChunkStream(enum.Enum):
    WEBCAM = "webcam"
    FEATURES = "features"
    EVENTS = "events"
    STIMULUS_FRAMES = "stimulus_frames"


class JobKind(enum.Enum):
    PROCESS_SESSION = "process_session"
    ANALYZE_VIDEO = "analyze_video"
    COMPARE_PIPELINES = "compare_pipelines"
    RENDER_DEBUG = "render_debug"


class JobStatus(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Study(Base):
    __tablename__ = "studies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    mode: Mapped[StudyMode] = mapped_column(str_enum(StudyMode, "study_mode"), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(Json, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    duration: Mapped[float] = mapped_column(Float, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    fps: Mapped[float] = mapped_column(Float, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    aois: Mapped[dict[str, Any] | None] = mapped_column(Json, nullable=True)


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    screen_width_cm: Mapped[float] = mapped_column(Float, nullable=False)
    screen_height_cm: Mapped[float] = mapped_column(Float, nullable=False)
    viewing_distance_cm: Mapped[float] = mapped_column(Float, nullable=False)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    study_id: Mapped[int] = mapped_column(ForeignKey("studies.id"), nullable=False, index=True)
    participant_code: Mapped[str] = mapped_column(String, nullable=False)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    upload_token_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        str_enum(SessionStatus, "session_status"), nullable=False, default=SessionStatus.CREATED
    )
    consent_version: Mapped[str | None] = mapped_column(String, nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_info: Mapped[dict[str, Any] | None] = mapped_column(Json, nullable=True)
    quality: Mapped[dict[str, Any] | None] = mapped_column(Json, nullable=True)
    raw_video_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_debug: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flags: Mapped[list[str]] = mapped_column(Json, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Chunk(Base):
    __tablename__ = "chunks"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    stream: Mapped[ChunkStream] = mapped_column(
        str_enum(ChunkStream, "chunk_stream"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind, "job_kind"), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(Json, nullable=False, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "job_status"), nullable=False, default=JobStatus.QUEUED, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    log: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String, primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
