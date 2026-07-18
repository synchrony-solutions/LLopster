"""add investigation stage columns to run

Phase B of model-tier routing: after Loki/Prom collection, a Sonnet
call produces an Investigation (root-cause hypothesis + affected files
list) that Phase C will use to narrow Opus's codebase context. In
Phase B these columns are populated but not yet consumed by the Opus
synthesis call — purely additive.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-25 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_NEW_COLUMNS = [
    ("investigation_root_cause", sa.Text()),
    # JSON list of file paths (relative to codebase_path) Sonnet flagged
    # as likely-affected. Phase C will use this to slice the codebase
    # blob Opus sees.
    ("investigation_affected_files_json", sa.JSON()),
    ("investigation_confidence", sa.Integer()),
    ("investigation_reasoning", sa.Text()),
    ("investigation_response_text", sa.Text()),
    ("investigation_model", sa.String(length=128)),
    ("investigation_input_tokens", sa.Integer()),
    ("investigation_output_tokens", sa.Integer()),
    ("investigation_cache_read_tokens", sa.Integer()),
    ("investigation_cache_creation_tokens", sa.Integer()),
    ("investigation_latency_ms", sa.Integer()),
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
