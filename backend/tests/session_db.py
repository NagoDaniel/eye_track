"""Put the recorded fixture session into the database (study, session, chunk rows)."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.models import Chunk, ChunkStream, SessionStatus, Study, StudyMode
from app.models import Session as SessionRow
from tests.session_fixture import FixtureSession


def seed_session(
    engine: Engine,
    s: FixtureSession,
    status: SessionStatus = SessionStatus.UPLOADED,
    flags: list[str] | None = None,
) -> None:
    with Db(engine) as db:
        db.add(Study(id=s.study_id, name="S", code="abc", mode=StudyMode.REMOTE, config={}))
        db.flush()
        db.add(
            SessionRow(
                id=s.session_id, study_id=s.study_id, participant_code="AB0315",
                upload_token_hash="h", status=status, flags=flags or [],
            )
        )  # fmt: skip
        db.flush()
        for r in s.manifest:
            db.add(Chunk(session_id=s.session_id, stream=ChunkStream(r.stream), seq=r.seq,
                         size=r.size, sha256=r.sha256))  # fmt: skip
        db.commit()
