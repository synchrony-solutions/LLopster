"""add triage stage columns to run

Phase A of the model-tier routing work: a cheap Haiku call decides
whether to spend Opus tokens at all. These columns capture the
decision + reasoning + Haiku's token cost as a separate stage from the
existing synthesis-stage token columns.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-25 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_NEW_COLUMNS = [
    ("triage_decision", sa.String(length=16)),
    ("triage_confidence", sa.Integer()),
    ("triage_reasoning", sa.Text()),
    ("triage_model", sa.String(length=128)),
    ("triage_input_tokens", sa.Integer()),
    ("triage_output_tokens", sa.Integer()),
    ("triage_cache_read_tokens", sa.Integer()),
    ("triage_cache_creation_tokens", sa.Integer()),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    missing = [(name, col_type) for name, col_type in _NEW_COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table("runs") as batch_op:
        for name, col_type in missing:
            batch_op.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        for name, _ in _NEW_COLUMNS:
            batch_op.drop_column(name)
