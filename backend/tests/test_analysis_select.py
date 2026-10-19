"""Which sessions an analysis uses and why the others are left out (PLAN §9)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.models import Session as SessionRow
from app.models import SessionStatus, Study, StudyMode
from app.storage import derived_dir
from worker.analysis.select import (
    GAZE_FILE_MISSING,
    NOT_PROCESSED,
    SUPERSEDED,
    VIDEO_NOT_WATCHED,
    SessionInfo,
    query_sessions,
    select_sessions,
)
from worker.pipeline.gaze import GAZE_FILE

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def quality(
    watched: dict[str, int] | None = None, included: bool = True, reasons: list[str] | None = None
) -> dict[str, Any]:
    per_video = {
        k: {"frames": n, "valid_fraction": 0.9} for k, n in (watched or {"1": 300}).items()
    }
    return {
        "playback": {"per_video": per_video},
        "inclusion": {"included": included, "reasons": reasons or []},
    }


def info(
    n: int,
    code: str = "AB0315",
    minutes: int = 0,
    status: SessionStatus = SessionStatus.PROCESSED,
    q: dict[str, Any] | None = None,
    has_gaze: bool = True,
) -> SessionInfo:
    return SessionInfo(
        id=uuid.UUID(int=n),
        participant_code=code,
        created_at=T0 + timedelta(minutes=minutes),
        status=status,
        quality=q if q is not None else quality(),
        has_gaze=has_gaze,
    )


def reasons_of(selection: Any) -> dict[int, list[str]]:
    return {e.session_id.int: e.reasons for e in selection.excluded}


def test_an_included_session_that_watched_the_video_is_used() -> None:
    selection = select_sessions([info(1)], video_id=1)
    assert [i.id.int for i in selection.included] == [1] and selection.excluded == []


def test_each_exclusion_has_its_reason() -> None:
    infos = [
        info(1, "A", status=SessionStatus.UPLOADED, q=None),
        info(2, "B", status=SessionStatus.FAILED, q=None),
        info(3, "C", q=quality({"2": 300})),
        info(4, "D", q=quality({"1": 0, "2": 300})),
        info(5, "E", q=quality(included=False, reasons=["valid_fraction_low", "validation_error"])),
        info(6, "F", has_gaze=False),
        info(7, "G"),
    ]
    selection = select_sessions(infos, video_id=1)
    assert [i.id.int for i in selection.included] == [7]
    assert reasons_of(selection) == {
        1: [NOT_PROCESSED],
        2: [NOT_PROCESSED],
        3: [VIDEO_NOT_WATCHED],
        4: [VIDEO_NOT_WATCHED],
        5: ["valid_fraction_low", "validation_error"],
        6: [GAZE_FILE_MISSING],
    }


def test_the_video_decides_who_watched() -> None:
    infos = [info(1, "A", q=quality({"1": 100})), info(2, "B", q=quality({"2": 100}))]
    assert [i.id.int for i in select_sessions(infos, 1).included] == [1]
    assert [i.id.int for i in select_sessions(infos, 2).included] == [2]


def test_one_participant_counts_once_with_the_latest_included_session() -> None:
    infos = [
        info(1, "A", minutes=0),
        info(3, "A", minutes=20),
        info(2, "A", minutes=10),
        info(4, "A", minutes=30, q=quality(included=False, reasons=["debug_session"])),
        info(5, "B", minutes=5),
    ]
    selection = select_sessions(infos, 1)
    assert sorted(i.id.int for i in selection.included) == [3, 5]
    assert reasons_of(selection) == {1: [SUPERSEDED], 2: [SUPERSEDED], 4: ["debug_session"]}


def test_the_selection_does_not_depend_on_the_input_order() -> None:
    infos = [info(n, "AB"[n % 2], minutes=n) for n in range(6)]
    first = select_sessions(infos, 1)
    second = select_sessions(list(reversed(infos)), 1)
    assert sorted(i.id for i in first.included) == sorted(i.id for i in second.included)
    assert sorted(first.excluded, key=lambda e: e.session_id) == sorted(
        second.excluded, key=lambda e: e.session_id
    )


def test_query_reads_the_study_sessions_from_postgres(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    with Db(migrated_engine) as db:
        for study_id in (1, 2):
            db.add(
                Study(id=study_id, name="S", code=f"c{study_id}", mode=StudyMode.REMOTE, config={})
            )
        db.flush()
        rows = {
            1: (1, SessionStatus.PROCESSED, quality()),
            2: (1, SessionStatus.UPLOADED, None),
            3: (1, SessionStatus.WITHDRAWN, quality()),
            4: (1, SessionStatus.RECORDING, None),
            5: (2, SessionStatus.PROCESSED, quality()),
        }
        for n, (study_id, status, q) in rows.items():
            db.add(
                SessionRow(
                    id=uuid.UUID(int=n),
                    study_id=study_id,
                    participant_code=f"P{n}",
                    upload_token_hash="h",
                    status=status,
                    quality=q,
                    flags=[],
                )  # fmt: skip
            )
        db.commit()
    gaze = derived_dir(tmp_path, 1, uuid.UUID(int=1)) / GAZE_FILE
    gaze.parent.mkdir(parents=True)
    gaze.write_bytes(b"x")

    with Db(migrated_engine) as db:
        infos = query_sessions(db, tmp_path, study_id=1)
    assert {i.id.int: i.has_gaze for i in infos} == {1: True, 2: False}  # not 3, 4 (no data), 5

    selection = select_sessions(infos, video_id=1)
    assert [i.id.int for i in selection.included] == [1]
    assert reasons_of(selection) == {2: [NOT_PROCESSED]}
