"""add dedup_key column to run

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    if "dedup_key" not in existing:
        with op.batch_alter_table("runs") as batch_op:
            batch_op.add_column(sa.Column("dedup_key", sa.String(128), nullable=True))
            batch_op.create_index("ix_runs_dedup_key", ["dedup_key"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index("ix_runs_dedup_key")
        batch_op.drop_column("dedup_key")
