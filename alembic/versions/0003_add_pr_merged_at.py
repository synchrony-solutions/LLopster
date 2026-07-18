"""add pr_merged_at column to run

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-23 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    if "pr_merged_at" not in existing:
        with op.batch_alter_table("runs") as batch_op:
            batch_op.add_column(
                sa.Column("pr_merged_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("pr_merged_at")
