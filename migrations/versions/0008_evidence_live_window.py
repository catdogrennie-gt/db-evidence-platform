"""Match bounded live per-instance windows without sorting retained history."""
from alembic import op
from sqlalchemy import inspect

revision = "0008_evidence_live_window"
down_revision = "0007_demo"
branch_labels = None
depends_on = None

NAME = "ix_evidence_instance_mode_time"


def _online(statement):
    if op.get_context().as_sql:
        op.execute("SET @dbe_previous_lock_wait_timeout = @@SESSION.lock_wait_timeout")
        op.execute("SET SESSION lock_wait_timeout=5")
        op.execute(statement)
        op.execute("SET SESSION lock_wait_timeout=@dbe_previous_lock_wait_timeout")
        return
    connection = op.get_bind()
    previous = int(connection.exec_driver_sql("SELECT @@SESSION.lock_wait_timeout").scalar_one())
    connection.exec_driver_sql("SET SESSION lock_wait_timeout=5")
    try:
        op.execute(statement)
    finally:
        connection.exec_driver_sql(f"SET SESSION lock_wait_timeout={previous}")


def upgrade():
    # The online expand step may precede application rollout. Only adopt the
    # exact additive index; a name collision must not silently hide drift.
    if not op.get_context().as_sql:
        for index in inspect(op.get_bind()).get_indexes("evidence"):
            if index["name"] == NAME:
                if index["column_names"] != ["instance_id", "source_mode", "observed_at", "id"] or index["unique"]:
                    raise RuntimeError("evidence live-window index definition mismatch")
                if op.get_context().dialect.name == "mysql":
                    rows = op.get_bind().exec_driver_sql(
                        "SHOW INDEX FROM evidence WHERE Key_name=%s", (NAME,)
                    ).mappings().all()
                    rows = sorted(rows, key=lambda row: row["Seq_in_index"])
                    if len(rows) != 4 or [row["Column_name"] for row in rows] != index["column_names"] or not all(
                        row["Sub_part"] is None and row["Collation"] == "A"
                        and row["Visible"] == "YES" and row["Index_type"] == "BTREE"
                        and row["Non_unique"] == 1 for row in rows
                    ):
                        raise RuntimeError("evidence live-window index definition mismatch")
                return
    if op.get_context().dialect.name == "mysql":
        _online(f"ALTER TABLE evidence ADD INDEX {NAME} (instance_id, source_mode, observed_at, id), ALGORITHM=INPLACE, LOCK=NONE")
    else:
        op.create_index(NAME, "evidence", ["instance_id", "source_mode", "observed_at", "id"])


def downgrade():
    if op.get_context().dialect.name == "mysql":
        _online(f"ALTER TABLE evidence DROP INDEX {NAME}, ALGORITHM=INPLACE, LOCK=NONE")
    else:
        op.drop_index(NAME, table_name="evidence")
