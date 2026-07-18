"""add synthesis_used_narrowed_context to run

Phase C of model-tier routing: Opus reads Sonnet's
`investigation_affected_files_json` and ships only those files in
its codebase blob. This boolean records whether the narrowed path
was actually taken — not just "was investigation present" but "did
PatchGenerator successfully build a non-empty narrowed blob from the
affected files".  Necessary because the empty-blob safety net can
flip us back to the full codebase mid-run; the dashboard / future
stats need a single source of truth on which mode Opus actually saw.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-25 00:00:02.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    if "synthesis_used_narrowed_context" not in existing:
        with op.batch_alter_table("runs") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "synthesis_used_narrowed_context",
                    sa.Boolean(),
                    nullable=True,
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("synthesis_used_narrowed_context")
