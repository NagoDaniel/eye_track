import json
import logging

import pytest

from app.logging import bind_context, configure_logging


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    out = capsys.readouterr().err.strip().splitlines()
    return [json.loads(line) for line in out]


def test_line_is_json_with_core_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("t").info("hello %s", "world")
    (rec,) = lines(capsys)
    assert rec["msg"] == "hello world"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "t"
    assert "ts" in rec


def test_context_fields_present_and_scoped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = logging.getLogger("t")
    with bind_context(session_id="s1", job_id="j1", stage="ingest"):
        log.info("inside")
    log.info("outside")
    inside, outside = lines(capsys)
    assert (inside["session_id"], inside["job_id"], inside["stage"]) == ("s1", "j1", "ingest")
    assert "session_id" not in outside and "job_id" not in outside and "stage" not in outside


def test_nested_context_merges(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    with bind_context(session_id="s1"), bind_context(stage="sync"):
        logging.getLogger("t").info("x")
    (rec,) = lines(capsys)
    assert rec["session_id"] == "s1" and rec["stage"] == "sync"


def test_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    logging.getLogger("t").info("quiet")
    logging.getLogger("t").warning("loud")
    (rec,) = lines(capsys)
    assert rec["msg"] == "loud"


def test_exception_is_serialized(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    (rec,) = lines(capsys)
    assert "ValueError: boom" in str(rec["exc"])


def test_invalid_level_fails_loudly() -> None:
    with pytest.raises(ValueError):
        configure_logging("LOUD")
