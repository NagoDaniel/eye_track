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
    PARTICIPANT_CODE_PATTERN,
    ChunkAck,
    ClientErrorRequest,
    CompleteRequest,
    CompleteResponse,
    ConsentRequest,
    ConsentResponse,
    EventRecord,
    SessionCreateRequest,
    SessionCreateResponse,
    StudyConfig,
    StudyInfo,
)

PROTOCOL: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "protocol.json").read_text()
)
EXAMPLES: dict[str, Any] = PROTOCOL["examples"]


def test_names_match_shared_protocol() -> None:
    assert list(get_args(schemas.Stream)) == PROTOCOL["streams"]
    assert list(get_args(schemas.EventType)) == PROTOCOL["event_types"]
    assert list(BROWSER_REQUIREMENTS) == PROTOCOL["browser_requirements"]
    assert PROTOCOL["participant_code_pattern"] == PARTICIPANT_CODE_PATTERN
    assert [s.value for s in ChunkStream] == PROTOCOL["streams"]  # DB enum agrees too


def test_default_config_is_the_plan_4_1_example() -> None:
    assert StudyConfig().model_dump(mode="json") == PROTOCOL["study_config_default"]


def test_partial_config_gets_plan_defaults() -> None:
    cfg = StudyConfig.model_validate({"calibration": {"points": 9}, "playlist": ["3", "4"]})
    assert cfg.calibration.points == 9
    assert cfg.calibration.dot_duration_ms == 1500
    assert cfg.calibration_gate.max_mean_error_norm == 0.10
    assert cfg.playlist == ["3", "4"]


@pytest.mark.parametrize(
    "bad",
    [
        {"calibration": {"points": 0}},
        {"calibration": {"margin": 0.5}},
        {"calibration_gate": {"on_fail": "ignore"}},
        {"calibration_gate": {"max_attempts": 0}},
        {"inclusion": {"min_valid_fraction": 1.5}},
        {"retention": {"raw_video_days_after_processing": -1}},
        {"playlist": [1]},  # ids are strings
        {"calibraton": {}},  # typo: unknown keys are errors, not ignored
        {"calibration": {"pointz": 5}},
        {"consent_version": ""},
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
