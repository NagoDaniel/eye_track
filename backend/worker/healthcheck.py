"""Container healthcheck for the worker: exit 0 if some worker beat recently (PLAN §11.1)."""

import sys
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import WorkerHeartbeat
from app.settings import Settings


def heartbeat_is_fresh(db: Session, max_age_s: float, now: datetime | None = None) -> bool:
    latest: datetime | None = db.scalar(select(func.max(WorkerHeartbeat.beat_at)))
    if latest is None:
        return False
    return ((now or datetime.now(UTC)) - latest).total_seconds() <= max_age_s


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    factory = make_session_factory(make_engine(settings.database_url))
    with factory() as db:
        fresh = heartbeat_is_fresh(db, settings.worker_heartbeat_max_age_s)
    if not fresh:
        print("worker heartbeat is stale or missing", file=sys.stderr)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
