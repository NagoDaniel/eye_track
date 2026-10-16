"""Study config and API models against `shared/protocol.json` (PLAN §1, §4.1, §5.9, §6)."""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

from app import schemas
from app.models import ChunkStream
from app.schemas import (
    BROWSER_REQUIREMENTS,
    CALIBRATION_POINT_COUNT,
    EVENT_DATA_MODELS,
    FEATURE_MATRIX_SIZE,
    INNER_POINT_LOW,
    LANDMARK_COUNT,
    PARTICIPANT_CODE_PATTERN,
    VALIDATION_POINT_COUNT,
    CalibGateResultData,
    ChunkAck,
    ClientErrorRequest,
    CompleteRequest,
    CompleteResponse,
    ConsentRequest,
    ConsentResponse,
    EventRecord,
    FeatureRecord,
    SessionCreateRequest,
    SessionCreateResponse,
    StimulusFrameRecord,
    StudyConfig,
    StudyInfo,
)

PROTOCOL: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "protocol.json").read_text()
)
EXAMPLES: dict[str, Any] = PROTOCOL["examples"]
LANDMARK_SUBSET: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "landmark_subset.json").read_text()
)


def test_names_match_shared_protocol() -> None:
    assert list(get_args(schemas.Stream)) == PROTOCOL["streams"]
    assert list(get_args(schemas.EventType)) == PROTOCOL["event_types"]
    assert list(BROWSER_REQUIREMENTS) == PROTOCOL["browser_requirements"]
    assert PROTOCOL["participant_code_pattern"] == PARTICIPANT_CODE_PATTERN
    assert PROTOCOL["calibration_point_count"] == CALIBRATION_POINT_COUNT
    assert PROTOCOL["validation_point_count"] == VALIDATION_POINT_COUNT
    assert PROTOCOL["inner_point_low"] == INNER_POINT_LOW
    assert [s.value for s in ChunkStream] == PROTOCOL["streams"]  # DB enum agrees too
    assert PROTOCOL["landmark_count"] == LANDMARK_COUNT
    assert PROTOCOL["feature_matrix_size"] == FEATURE_MATRIX_SIZE
    values = PROTOCOL["event_data_values"]
    assert list(get_args(schemas.GateBlock)) == values["gate_blocks"]
    assert list(get_args(schemas.GateReason)) == values["gate_reasons"]
    assert list(get_args(schemas.SkipTarget)) == values["skip_targets"]


def test_landmark_subset_file_is_consistent() -> None:
    indices = [i for group in LANDMARK_SUBSET["groups"].values() for i in group]
    assert len(indices) == LANDMARK_COUNT == PROTOCOL["landmark_count"]
    assert len(set(indices)) == len(indices)  # no landmark twice
    assert all(0 <= i < LANDMARK_SUBSET["mesh_size"] for i in indices)
    assert LANDMARK_SUBSET["mesh_size"] == 478  # 468 face points + 2 x 5 iris points
    assert set(range(468, 478)) <= set(indices)  # both iris rings (PLAN §5.4)


def test_default_config_is_the_plan_4_1_example() -> None:
    assert StudyConfig().model_dump(mode="json") == PROTOCOL["study_config_default"]


def test_partial_config_gets_plan_defaults() -> None:
    cfg = StudyConfig.model_validate({"calibration": {"margin": 0.1}, "playlist": ["3", "4"]})
    assert cfg.calibration.points == 13 and cfg.calibration.margin == 0.1
    assert cfg.calibration.dot_duration_ms == 1500
    assert cfg.calibration_gate.max_mean_error_norm == 0.10
    assert cfg.playlist == ["3", "4"]


@pytest.mark.parametrize(
    "bad",
    [
        {"calibration": {"points": 0}},
        {"calibration": {"points": 9}},  # the app supports exactly 13
        {"validation": {"points": 9}},  # and exactly 5
        {"calibration": {"margin": 0.25}},  # the grid margin must stay below the 25 % points
        {"calibration": {"margin": 0}},
        {"calibration": {"margin": 0.5}},
        {"calibration_gate": {"on_fail": "ignore"}},
        {"calibration_gate": {"max_attempts": 0}},
        {"inclusion": {"min_valid_fraction": 1.5}},
        {"retention": {"raw_video_days_after_processing": -1}},
        {"playlist": [1]},  # ids are strings
        {"calibraton": {}},  # typo: unknown keys are errors, not ignored
        {"calibration": {"pointz": 5}},
        {"consent_version": ""},
        {"quality": {"brightness_min": 0.9, "brightness_max": 0.5}},  # bounds must be ordered
        {"quality": {"face_ratio_min": 0.2, "face_ratio_max": 0.2}},
        {"quality": {"min_face_ratio_warn": 1.3}},  # above the default max_face_ratio_warn
        {"quality": {"brightness_max": 1.5}},
        {"quality": {"max_yaw_deg": 0}},
        {"quality": {"max_pitch_deg": 120}},
        {"quality": {"max_yaw": 20}},  # typo: unknown key
    ],
)
def test_invalid_config_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(bad)


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("study_info", StudyInfo),
        ("session_create_request", SessionCreateRequest),
        ("session_create_response", SessionCreateResponse),
        ("consent_request", ConsentRequest),
        ("consent_response", ConsentResponse),
        ("chunk_ack", ChunkAck),
        ("complete_request", CompleteRequest),
        ("complete_response_missing", CompleteResponse),
        ("complete_response_done", CompleteResponse),
        ("client_error_request", ClientErrorRequest),
        ("event_record", EventRecord),
        ("feature_record", FeatureRecord),
        ("feature_record_no_face", FeatureRecord),
        ("feature_record_unmeasured_brightness", FeatureRecord),
        ("stimulus_frame_record", StimulusFrameRecord),
    ],
)
def test_example_validates_and_round_trips(name: str, model: type[BaseModel]) -> None:
    parsed = model.model_validate(EXAMPLES[name])
    assert parsed.model_dump(mode="json") == EXAMPLES[name]


def test_event_type_must_be_known() -> None:
    with pytest.raises(ValidationError):
        EventRecord.model_validate({"t": 1.0, "type": "mystery", "data": {}})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("AB0315", "AB0315"), ("ab0315", "AB0315"), ("  zs1231 ", "ZS1231"), ("XY0229", "XY0229")],
)
def test_participant_code_accepted_and_upper_cased(raw: str, expected: str) -> None:
    req = SessionCreateRequest(study_code="s", participant_code=raw)
    assert req.participant_code == expected


@pytest.mark.parametrize(
    "raw",
    ["", "A0315", "ABC0315", "AB1315", "AB0015", "AB0332", "AB0300", "ÁB0315", "AB031", "1B0315"],
)
def test_participant_code_rejected(raw: str) -> None:
    with pytest.raises(ValidationError, match="participant code"):
        SessionCreateRequest(study_code="s", participant_code=raw)


def test_complete_request_rejects_unknown_stream() -> None:
    with pytest.raises(ValidationError):
        CompleteRequest.model_validate({"last_seq": {"video": 3}})


def test_session_create_request_debug_defaults_to_false() -> None:
    req = SessionCreateRequest.model_validate({"study_code": "s", "participant_code": "AB0315"})
    assert req.debug is False
    debug = SessionCreateRequest.model_validate(
        {"study_code": "s", "participant_code": "AB0315", "debug": True}
    )
    assert debug.debug is True


def test_partial_quality_config_keeps_defaults() -> None:
    cfg = StudyConfig.model_validate({"quality": {"brightness_min": 0.3}})
    assert cfg.quality.brightness_min == 0.3
    assert cfg.quality.brightness_max == 0.85
    assert cfg.quality.min_face_ratio_warn == 0.8


@pytest.mark.parametrize("event_type", sorted(EVENT_DATA_MODELS))
def test_event_data_example_validates(event_type: str) -> None:
    assert event_type in PROTOCOL["event_types"]
    data = EXAMPLES["event_data"][event_type]
    parsed = EVENT_DATA_MODELS[event_type].model_validate(data)
    assert parsed.model_dump(mode="json") == data


def test_every_example_event_data_has_a_model() -> None:
    assert sorted(EXAMPLES["event_data"]) == sorted(EVENT_DATA_MODELS)


def test_example_feature_record_matches_the_subset() -> None:
    record = EXAMPLES["feature_record"]
    assert len(record["lm"]) == LANDMARK_COUNT
    assert len(record["matrix"]) == FEATURE_MATRIX_SIZE


@pytest.mark.parametrize(
    "change",
    [
        {"lm": EXAMPLES["feature_record"]["lm"][:-1]},  # one landmark short
        {"matrix": [1.0] * 15},
        {"lm": [[0.1, 0.2]] * 49},  # not x, y, z
        {"blink_l": None},  # a face needs every measurement except brightness
        {"face": False},  # no face but measurements present
        {"presented_frames": -1},
        {"extra": 1},
    ],
)
def test_invalid_feature_record_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FeatureRecord.model_validate({**EXAMPLES["feature_record"], **change})


def test_a_face_frame_may_have_an_unknown_brightness_but_a_no_face_frame_may_not_have_one() -> None:
    record = FeatureRecord.model_validate({**EXAMPLES["feature_record"], "brightness": None})
    assert record.face and record.brightness is None
    with pytest.raises(ValidationError):
        FeatureRecord.model_validate({**EXAMPLES["feature_record_no_face"], "brightness": 0.4})


def test_stimulus_frame_record_rejects_unknown_and_missing_fields() -> None:
    good = EXAMPLES["stimulus_frame_record"]
    with pytest.raises(ValidationError):
        StimulusFrameRecord.model_validate({**good, "videoId": "1"})
    with pytest.raises(ValidationError):
        StimulusFrameRecord.model_validate({k: v for k, v in good.items() if k != "media_time"})


def test_skipped_gate_result_has_no_verdict() -> None:
    base = {**EXAMPLES["event_data"]["calib_gate_result"], "reason": "no_validation"}
    skipped = CalibGateResultData.model_validate(
        {**base, "skipped": True, "passed": None, "mean_error_norm": None, "point_errors": []}
    )
    assert skipped.passed is None
    with pytest.raises(ValidationError):  # skipped but with a verdict
        CalibGateResultData.model_validate({**base, "skipped": True})
    with pytest.raises(ValidationError):  # scored but without a verdict
        CalibGateResultData.model_validate({**base, "passed": None})
    with pytest.raises(ValidationError):  # unknown block
        CalibGateResultData.model_validate({**base, "block": "middle"})
