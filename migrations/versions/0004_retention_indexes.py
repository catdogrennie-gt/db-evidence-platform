"""Lead with global TTL timestamps for bounded cleanup range scans."""

from alembic import op

revision = "0004_retention_indexes"
down_revision = "0003_collection_bundle"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_metric_frames_retention", "metric_frames", ["observed_at", "run_id"]),
    ("ix_digest_retention", "digest_snapshots", ["observed_at", "id"]),
    ("ix_manifests_retention", "evidence_manifests", ["retain_until", "id"]),
    ("ix_pins_retention", "evidence_pins", ["retain_until", "id"]),
)


def upgrade():
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
