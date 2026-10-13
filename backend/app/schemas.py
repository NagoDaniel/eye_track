"""Pydantic models for the study config (PLAN §4.1) and the participant API (PLAN §6).

The names in `Stream`, `EventType` and `BROWSER_REQUIREMENTS` are mirrored in
`frontend/src/{eventLog,config}.ts`; `shared/protocol.json` is the reference and both sides have a
test that compares against it (PLAN §1: single source of truth, PLAN §5.9).
"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Stream = Literal["webcam", "features", "events", "stimulus_frames"]

EventType = Literal[
    "session_start",
    "viewport_change",
    "fullscreen_change",
    "visibility_change",
    "window_blur",
    "window_focus",
    "calib_dot_show",
    "calib_dot_click",
    "calib_dot_hide",
    "calib_gate_result",
    "video_load",
    "video_play",
    "video_pause",
    "video_ended",
    "video_stall",
    "face_lost",
    "face_regained",
    "recorder_start",
    "recorder_dataavailable",
    "recorder_stop",
    "debug_skip",
    "client_error",
]

# Browser APIs the participant app needs (PLAN §5.1); the frontend maps each name to a check.
BROWSER_REQUIREMENTS: tuple[str, ...] = (
    "getUserMedia",
    "MediaRecorder",
    "requestVideoFrameCallback",
    "requestFullscreen",
    "IndexedDB",
    "WebAssembly",
)

# First two letters of the name + birth month + birth day (DECISIONS, milestone 1b item 11).
PARTICIPANT_CODE_PATTERN = r"^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$"
_PARTICIPANT_CODE_RE = re.compile(PARTICIPANT_CODE_PATTERN)


class Strict(BaseModel):
    """Base for every model here: unknown fields are errors, never silently dropped."""

    model_config = ConfigDict(extra="forbid")


# --- Study config (PLAN §4.1). Every default below is the plan's default. ---------------------


class CalibrationConfig(Strict):
    points: int = Field(default=13, gt=0)
    dot_duration_ms: int = Field(default=1500, gt=0)
    margin: float = Field(default=0.06, ge=0, lt=0.5)


class ValidationConfig(Strict):
    points: int = Field(default=5, gt=0)
    before_video: bool = True
    between_videos: bool = True
    after_last_video: bool = True


class CalibrationGateConfig(Strict):
    enabled: bool = True
    max_mean_error_norm: float = Field(default=0.10, gt=0)
    max_point_error_norm: float = Field(default=0.18, gt=0)
    min_valid_frames_per_dot: int = Field(default=5, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    on_fail: Literal["continue_flagged", "end_session"] = "continue_flagged"


class PlaybackConfig(Strict):
    allow_pause: bool = False
    require_fullscreen: bool = True


class RecordingConfig(Strict):
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    fps: int = Field(default=30, gt=0)
    chunk_ms: int = Field(default=2000, gt=0)


class QualityConfig(Strict):
    min_face_ratio_warn: float = Field(default=0.8, gt=0)
    max_face_ratio_warn: float = Field(default=1.25, gt=0)


class InclusionConfig(Strict):
    max_validation_error_norm: float = Field(default=0.12, gt=0)
    min_valid_fraction: float = Field(default=0.7, ge=0, le=1)


class RetentionConfig(Strict):
    # None: raw video is kept until manually deleted.
    raw_video_days_after_processing: int | None = Field(default=None, ge=0)


class StudyConfig(Strict):
    playlist: list[str] = Field(default_factory=list)  # video ids as strings (DECISIONS item 8)
    calibration: CalibrationConfig = Field(default_factory=lambda: CalibrationConfig())
    validation: ValidationConfig = Field(default_factory=lambda: ValidationConfig())
    calibration_gate: CalibrationGateConfig = Field(default_factory=lambda: CalibrationGateConfig())
    playback: PlaybackConfig = Field(default_factory=lambda: PlaybackConfig())
    recording: RecordingConfig = Field(default_factory=lambda: RecordingConfig())
    quality: QualityConfig = Field(default_factory=lambda: QualityConfig())
    inclusion: InclusionConfig = Field(default_factory=lambda: InclusionConfig())
    retention: RetentionConfig = Field(default_factory=lambda: RetentionConfig())
    consent_version: str = Field(default="v1", min_length=1)
    debug_allowed: bool = False


# --- Participant API (PLAN §6) -----------------------------------------------------------------


class StudyInfo(Strict):
    """`GET /api/studies/{code}`."""

    name: str
    code: str
    mode: Literal["remote", "lab"]
    consent_version: str
    consent_text: str
    browser_requirements: list[str]


class VideoRef(Strict):
    id: str
    url: str
    sha256: str
    duration: float


class SessionCreateRequest(Strict):
    """`POST /api/sessions`. The participant code is upper-cased before it is checked."""

    study_code: str
    participant_code: str

    @field_validator("participant_code", mode="before")
    @classmethod
    def normalize_participant_code(cls, value: object) -> object:
        code = value.strip().upper() if isinstance(value, str) else value
        if not isinstance(code, str) or not _PARTICIPANT_CODE_RE.fullmatch(code):
            raise ValueError(
                "participant code must be two letters, then birth month and day, e.g. AB0315"
            )
        return code


class SessionCreateResponse(Strict):
    session_id: str
    upload_token: str
    config: StudyConfig
    videos: list[VideoRef]


class ConsentRequest(Strict):
    consent_version: str


class ConsentResponse(Strict):
    status: Literal["consented"]


class ChunkAck(Strict):
    """Reply to `PUT /api/sessions/{id}/chunks/{stream}/{seq}`; the same reply for a repeat."""

    stream: Stream
    seq: int
    size: int


class CompleteRequest(Strict):
    """`POST /api/sessions/{id}/complete`: the highest seq the client sent, per stream."""

    last_seq: dict[Stream, Annotated[int, Field(ge=0)]]


class CompleteResponse(Strict):
    status: Literal["uploaded", "incomplete"]
    missing: dict[Stream, list[int]]


class ClientErrorRequest(Strict):
    """`POST /api/client-errors` (no authentication; the session is optional)."""

    message: str
    stack: str | None = None
    url: str | None = None
    t: float | None = None
    session_id: str | None = None


class EventRecord(Strict):
    """One line of the `events` stream (PLAN §5.9)."""

    t: float
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
