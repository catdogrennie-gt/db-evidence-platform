"""Durable singleton demo control and run history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0007_demo"
down_revision = "0006_inspection"
branch_labels = None
depends_on = None
UTC = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade():
    op.create_table(
        "demo_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("current_run_id", sa.String(36)),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(36)),
        sa.Column("lease_until", UTC),
        sa.Column("cooldown_until", UTC),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "demo_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(36), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("requested_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        *[
            sa.Column(k, sa.String(n), nullable=False)
            for k, n in [
                ("status", 30),
                ("phase", 40),
                ("fault_state", 30),
                ("analysis_status", 40),
                ("source_id", 36),
                ("lock_itemid", 80),
                ("lock_hostid", 80),
            ]
        ],
        *[
            sa.Column(k, UTC, nullable=k != "started_at")
            for k in [
                "started_at",
                "expires_at",
                "stopped_at",
                "fault_confirmed_at",
                "cleanup_started_at",
                "observed_at",
                "ai_reserved_at",
                "next_collection_at",
            ]
        ],
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("evidence_id", sa.String(36)),
        sa.Column("investigation_id", sa.String(36), sa.ForeignKey("investigations.id")),
        sa.Column("error_code", sa.String(80)),
        sa.Column("cleanup", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_demo_runs_started", "demo_runs", ["started_at"])
    op.bulk_insert(
        sa.table(
            "demo_control",
            sa.column("id", sa.Integer()),
            sa.column("fence", sa.Integer()),
            sa.column("blocked", sa.Boolean()),
        ),
        [dict(id=1, fence=0, blocked=False)],
    )


def downgrade():
    op.drop_table("demo_runs")
    op.drop_table("demo_control")
