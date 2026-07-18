"""add operator ground-truth label columns to run

The data-collection half of the eval / ground-truth flywheel (ROADMAP
Track B): operators judge each run "correct / wrong / partial / na" from
the dashboard's run-detail page. Every labeled run becomes a regression
case for the eval harness, so this is a compounding, proprietary dataset
— the columns land first so labels start accumulating before the harness
that consumes them exists.

`operator_label` is indexed because the future harness pulls labeled runs
by label; the others are plain nullable columns.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-13 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("operator_label", sa.String(length=16)),
    ("operator_label_note", sa.Text()),
    ("operator_labeled_at", sa.DateTime(timezone=True)),
    ("operator_labeled_by", sa.String(length=64)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    with op.batch_alter_table("runs") as batch_op:
        for name, type_ in _COLUMNS:
            if name not in existing:
                batch_op.add_column(sa.Column(name, type_, nullable=True))
    # Index on the label so the eval harness can filter labeled runs cheaply.
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("runs")}
    if "ix_runs_operator_label" not in existing_indexes:
        op.create_index("ix_runs_operator_label", "runs", ["operator_label"])


def downgrade() -> None:
    op.drop_index("ix_runs_operator_label", table_name="runs")
    with op.batch_alter_table("runs") as batch_op:
        for name, _type in _COLUMNS:
            batch_op.drop_column(name)
