"""Pydantic models for the study config (PLAN §4.1) and the participant API (PLAN §6).

The names in `Stream`, `EventType` and `BROWSER_REQUIREMENTS` are mirrored in
`frontend/src/{eventLog,config}.ts`; `shared/protocol.json` is the reference and both sides have a
test that compares against it (PLAN §1: single source of truth, PLAN §5.9).
"""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

# The participant app supports exactly these point layouts (PLAN §5.5); mirrored in
# frontend/src/constants.ts and checked against shared/protocol.json, so a study the app cannot run
# is rejected when it is saved. The grid margin must stay below the inner points at 25 %.
CALIBRATION_POINT_COUNT = 13
VALIDATION_POINT_COUNT = 5
INNER_POINT_LOW = 0.25

# Size of the landmark subset in a feature record and of the transformation matrix (PLAN §5.4);
# mirrored in frontend/src/constants.ts and checked against shared/landmark_subset.json.
LANDMARK_COUNT = 49
FEATURE_MATRIX_SIZE = 16

# Values of the enum-like fields in event data (PLAN §5.6, §12.1); the lists are in
# shared/protocol.json under `event_data_values`.
GateBlock = Literal["before_video", "between_videos", "after_last_video"]
GateReason = Literal[
    "ok",
    "few_face_frames",
    "low_brightness",
    "head_or_distance_change",
    "high_error",
    "no_validation",
    "forced_debug",
]
SkipTarget = Literal["calibration", "validation", "video"]

# First two letters of the name + birth month + birth day (DECISIONS, milestone 1b item 11).
PARTICIPANT_CODE_PATTERN = r"^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$"
_PARTICIPANT_CODE_RE = re.compile(PARTICIPANT_CODE_PATTERN)


class Strict(BaseModel):
    """Base for every model here: unknown fields are errors, never silently dropped."""

    model_config = ConfigDict(extra="forbid")


# --- Study config (PLAN §4.1). Every default below is the plan's default. ---------------------


class CalibrationConfig(Strict):
    points: int = Field(default=CALIBRATION_POINT_COUNT)
    dot_duration_ms: int = Field(default=1500, gt=0)
    margin: float = Field(default=0.06, gt=0, lt=INNER_POINT_LOW)

    @field_validator("points")
    @classmethod
    def supported_points(cls, value: int) -> int:
        if value != CALIBRATION_POINT_COUNT:
            raise ValueError(
                f"the participant app supports exactly {CALIBRATION_POINT_COUNT} points"
            )
        return value


class ValidationConfig(Strict):
    points: int = Field(default=VALIDATION_POINT_COUNT)
    before_video: bool = True
    between_videos: bool = True
    after_last_video: bool = True

    @field_validator("points")
    @classmethod
    def supported_points(cls, value: int) -> int:
        if value != VALIDATION_POINT_COUNT:
            raise ValueError(
                f"the participant app supports exactly {VALIDATION_POINT_COUNT} points"
            )
        return value


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
    """Live quality limits.

    The two `*_warn` values are the plan's (§4.1), read as multipliers of the median face ratio of
    the device check; the rest are starting values, tuned on the pilot with the debug-mode sliders
    (DECISIONS, milestone 2 item 3).
    """

    min_face_ratio_warn: float = Field(default=0.8, gt=0)
    max_face_ratio_warn: float = Field(default=1.25, gt=0)
    # Mean luminance of the face region, 0 (black) to 1 (white).
    brightness_min: float = Field(default=0.25, ge=0, le=1)
    brightness_max: float = Field(default=0.85, ge=0, le=1)
    # Interocular distance as a fraction of the frame width; the band for roughly arm's length.
    face_ratio_min: float = Field(default=0.045, gt=0)
    face_ratio_max: float = Field(default=0.16, gt=0)
    # "Roughly frontal": head yaw and pitch from the transformation matrix, in degrees.
    max_yaw_deg: float = Field(default=20, gt=0, le=90)
    max_pitch_deg: float = Field(default=20, gt=0, le=90)

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> "QualityConfig":
        for low, high in (
            ("min_face_ratio_warn", "max_face_ratio_warn"),
            ("brightness_min", "brightness_max"),
            ("face_ratio_min", "face_ratio_max"),
        ):
            if getattr(self, low) >= getattr(self, high):
                raise ValueError(f"{low} must be below {high}")
        return self


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
    # Frames per second of the stimulus video: the face model runs at this rate (DECISIONS,
    # milestone 2 item 10).
    fps: float = Field(gt=0)


class SessionCreateRequest(Strict):
    """`POST /api/sessions`. The participant code is upper-cased before it is checked."""

    study_code: str
    participant_code: str
    # Asks for a debug session (PLAN §12.1); the server allows it only for studies with
    # `debug_allowed` or when an admin is logged in on that browser.
    debug: bool = False

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
    # True when the participant reloaded the page mid-session: the server flags `interrupted`.
    interrupted: bool = False


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


# --- Data streams (PLAN §5.3, §5.4) ------------------------------------------------------------

Landmark = Annotated[list[float], Field(min_length=3, max_length=3)]


class FeatureRecord(Strict):
    """One line of the `features` stream: a frame the live face model processed (PLAN §5.4).

    `t` is the client clock when the webcam frame was presented to the page; `webcam_media_time`,
    `capture_time` and `presented_frames` are the metadata of that `requestVideoFrameCallback`
    on the hidden webcam preview (§5.3). Without a face, every measurement is null.
    """

    t: float
    webcam_media_time: float
    capture_time: float | None
    presented_frames: int | None = Field(ge=0)
    face: bool
    lm: list[Landmark] | None
    matrix: list[float] | None
    blink_l: float | None
    blink_r: float | None
    face_ratio: float | None
    brightness: float | None

    @model_validator(mode="after")
    def measurements_match_face(self) -> "FeatureRecord":
        values = (
            self.lm,
            self.matrix,
            self.blink_l,
            self.blink_r,
            self.face_ratio,
            self.brightness,
        )
        if not self.face:
            if any(v is not None for v in values):
                raise ValueError("a frame without a face has no measurements")
            return self
        if any(v is None for v in values):
            raise ValueError("a frame with a face needs every measurement")
        if self.lm is not None and len(self.lm) != LANDMARK_COUNT:
            raise ValueError(f"lm must hold {LANDMARK_COUNT} landmarks")
        if self.matrix is not None and len(self.matrix) != FEATURE_MATRIX_SIZE:
            raise ValueError(f"matrix must hold {FEATURE_MATRIX_SIZE} numbers")
        return self


class StimulusFrameRecord(Strict):
    """One line of the `stimulus_frames` stream: a presented frame of the stimulus video (§5.3).

    The names are the snake_case form of the `requestVideoFrameCallback` metadata. The video is
    identified by the `video_*` events around it.
    """

    t: float
    expected_display_time: float
    media_time: float
    presented_frames: int = Field(ge=0)


# --- Event data (PLAN §5.4, §5.6, §12.1). Only the types listed in EVENT_DATA_MODELS are typed;
# the others keep a free-form `data` (see the frontend's KnownEventData). -------------------------


class RecorderStartData(Strict):
    mime_type: str
    bits_per_second: int = Field(gt=0)
    settings: dict[str, Any]  # `track.getSettings()` as the browser reports it


class RecorderDataAvailableData(Strict):
    seq: int = Field(ge=0)
    size: int = Field(ge=0)


class RecorderStopData(Strict):
    chunks: int = Field(ge=0)
    bytes: int = Field(ge=0)


class FaceLostData(Strict):
    pass


class FaceRegainedData(Strict):
    lost_ms: float = Field(ge=0)


class CalibGateResultData(Strict):
    """A scored validation block. Only `before_video` blocks can block the participant (§5.6)."""

    block: GateBlock
    attempt: int = Field(ge=1)
    passed: bool | None  # null when the gate was skipped
    skipped: bool  # true when it could not be scored (no validation block to score)
    mean_error_norm: float | None
    point_errors: list[float | None]  # per validation point; null when it had no usable frame
    valid_frames_per_dot: list[int]  # per calibration dot, in dot order
    reason: GateReason

    @model_validator(mode="after")
    def skipped_has_no_verdict(self) -> "CalibGateResultData":
        if self.skipped != (self.passed is None):
            raise ValueError("passed is null exactly when the gate was skipped")
        return self


class DebugSkipData(Strict):
    target: SkipTarget


EVENT_DATA_MODELS: dict[str, type[Strict]] = {
    "recorder_start": RecorderStartData,
    "recorder_dataavailable": RecorderDataAvailableData,
    "recorder_stop": RecorderStopData,
    "face_lost": FaceLostData,
    "face_regained": FaceRegainedData,
    "calib_gate_result": CalibGateResultData,
    "debug_skip": DebugSkipData,
}
