"""add eval_runs table for the ground-truth flywheel pass-rate trend

The consume half of the eval / ground-truth flywheel (ROADMAP Track B):
`Run.operator_label` (0007) is the *write* path — humans labeling real runs.
This table is the *score* path — each replay of the frozen scenario corpus
writes one timestamped row (pass-rate + per-scenario breakdown) so the
dashboard can show the moat as a growing trend, not a single number.

Idempotent like the rest of the chain: skips creation if the table already
exists, so the legacy-DB `upgrade head` path is safe on existing volumes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "eval_runs" in inspector.get_table_names():
        return
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corpus_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("trigger_source", sa.String(length=32), nullable=False, server_default="cli"),
        sa.Column("scenario_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correct_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partial_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wrong_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pass_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("results_json", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("ix_eval_runs_created_at", "eval_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_eval_runs_created_at", table_name="eval_runs")
    op.drop_table("eval_runs")
