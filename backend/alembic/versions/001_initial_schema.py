"""Initial schema — sessions, audit_log, checkpoints, learning_outcomes.

Revision ID: 001
Revises: None
Create Date: 2024-01-01 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- sessions ---
    op.create_table(
        "sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("build_mode", sa.VARCHAR(32), nullable=False),
        sa.Column("status", sa.VARCHAR(32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("context_json", JSONB, nullable=True),
    )

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.VARCHAR(64), nullable=False),
        sa.Column("source", sa.VARCHAR(128), nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("causation_id", sa.VARCHAR(128), nullable=True),
        sa.Column("correlation_id", sa.VARCHAR(128), nullable=True),
        sa.Column("event_id", sa.VARCHAR(128), unique=True, nullable=True),
    )
    op.create_unique_constraint(
        "uq_audit_log_session_seq", "audit_log", ["session_id", "seq"]
    )

    # --- checkpoints ---
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.VARCHAR(128), nullable=False),
        sa.Column("highest_seq", sa.Integer(), nullable=False),
        sa.Column("state_json", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_checkpoints_session_id", "checkpoints", ["session_id"])

    # --- learning_outcomes ---
    op.create_table(
        "learning_outcomes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.VARCHAR(128), nullable=False),
        sa.Column("outcome_status", sa.VARCHAR(32), nullable=False),
        sa.Column("data_json", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("learning_outcomes")
    op.drop_table("checkpoints")
    op.drop_table("audit_log")
    op.drop_table("sessions")
