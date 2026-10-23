"""job kind process_pipeline_b (Pipeline B runs as a job of its own, DECISIONS 2026-10-20)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 20:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = ("process_session", "analyze_video", "compare_pipelines", "render_debug")
NEW_KINDS = (*OLD_KINDS, "process_pipeline_b")


def _kinds(values: Sequence[str]) -> str:
    return "kind IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("job_kind", "jobs", type_="check")
    op.create_check_constraint("job_kind", "jobs", _kinds(NEW_KINDS))


def downgrade() -> None:
    # Pipeline B jobs are disposable: the constraint cannot hold them.
    op.execute("DELETE FROM jobs WHERE kind = 'process_pipeline_b'")
    op.drop_constraint("job_kind", "jobs", type_="check")
    op.create_check_constraint("job_kind", "jobs", _kinds(OLD_KINDS))
