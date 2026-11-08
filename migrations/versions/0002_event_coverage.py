"""Preserve event coverage revision and explicit closure semantics.

Revision ID: 0002_event_coverage
Revises: 0001_platform
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_event_coverage"
down_revision = "0001_platform"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "events", sa.Column("coverage_revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("events", sa.Column("closure_type", sa.String(40), nullable=True))
    op.add_column("events", sa.Column("closure_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("events", "closure_reason")
    op.drop_column("events", "closure_type")
    op.drop_column("events", "coverage_revision")
