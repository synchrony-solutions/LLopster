"""add cluster_state_json to run

Read-only Kubernetes cluster-state access (issue #23). Stores the serialized
ClusterState the agent collected for an alert: pod/container states, exit
codes and OOMKilled reasons, owner chain, events, PVC and node conditions.

Deliberately nullable with no default. NULL means cluster access was off for
that run — the shipping default — while a stored object with empty lists means
the agent looked and found nothing. Collapsing those two into `{}` would make
the run history unable to answer "did this run have cluster context?", which
is the first question when comparing outcomes before and after enabling it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-29 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns("runs")}
    if "cluster_state_json" not in existing:
        with op.batch_alter_table("runs") as batch_op:
            batch_op.add_column(sa.Column("cluster_state_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("cluster_state_json")
