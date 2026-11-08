"""One bounded task may collect several independently timed source profiles.

Revision ID: 0003_collection_bundle
Revises: 0002_event_coverage
"""

from alembic import op

revision = "0003_collection_bundle"
down_revision = "0002_event_coverage"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("collection_runs") as batch:
            batch.drop_constraint("uq_collection_runs_task_id", type_="unique")
            batch.create_unique_constraint(
                "uq_collection_runs_task_attempt_collector", ["task_id", "attempt", "collector"]
            )
        return
    op.create_unique_constraint(
        "uq_collection_runs_task_attempt_collector",
        "collection_runs",
        ["task_id", "attempt", "collector"],
    )
    op.drop_constraint("uq_collection_runs_task_id", "collection_runs", type_="unique")


def downgrade():
    # Downgrade is deliberately blocked while bundled runs would lose identity.
    connection = op.get_bind()
    from sqlalchemy import text

    duplicate = connection.execute(
        text(
            "SELECT task_id FROM collection_runs GROUP BY task_id,attempt HAVING COUNT(*)>1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError("Bundled collection runs exist; export/archive before downgrade")
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("collection_runs") as batch:
            batch.drop_constraint("uq_collection_runs_task_attempt_collector", type_="unique")
            batch.create_unique_constraint("uq_collection_runs_task_id", ["task_id", "attempt"])
        return
    op.create_unique_constraint(
        "uq_collection_runs_task_id", "collection_runs", ["task_id", "attempt"]
    )
    op.drop_constraint(
        "uq_collection_runs_task_attempt_collector", "collection_runs", type_="unique"
    )
