from pathlib import Path

from alembic.config import Config
from sqlalchemy import Engine

from alembic import command
from app.settings import Settings

BACKEND = Path(__file__).resolve().parent.parent


def alembic_config(engine: Engine) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    cfg = alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def downgrade(engine: Engine, revision: str = "base") -> None:
    cfg = alembic_config(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, revision)


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "secret_key": "s" * 32,
        "postgres_password": "pw",
        "admin_cookie_secure": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg, arg-type]
