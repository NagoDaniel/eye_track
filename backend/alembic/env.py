from sqlalchemy import Connection

from alembic import context
from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.db import Base, make_engine
from app.settings import Settings


def run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


# Tests pass an open connection via `config.attributes["connection"]`.
connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    with make_engine(Settings().database_url).connect() as conn:  # type: ignore[call-arg]
        run(conn)
        conn.commit()
