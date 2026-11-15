"""Actual MySQL access path with synthetic history; no production evidence."""
from datetime import datetime, timedelta
import json
from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, JSON, MetaData, String, Table, text
from sqlalchemy.schema import CreateTable

from platform_app import models as m
from tests.platform.test_background_mysql import mysql_background_engine


def test_live_window_uses_ordered_range_instead_of_sorting_retained_history(mysql_background_engine):
    engine = mysql_background_engine
    table = Table("regression_evidence_" + uuid4().hex[:12], MetaData(),
        Column("id", String(36), primary_key=True), Column("instance_id", String(36), nullable=False),
        Column("source", String(60), nullable=False), Column("source_mode", String(20), nullable=False),
        Column("observed_at", DateTime, nullable=False), Column("payload", JSON, nullable=False))
    # Use the actual model index definitions, only those whose columns belong
    # to this synthetic shape. No FK or runtime-schema grants are needed.
    for index in m.Evidence.__table__.indexes:
        columns = [c.name for c in index.columns]
        if set(columns).issubset(table.c.keys()):
            Index(index.name, *(table.c[name] for name in columns))
    # CREATE grants suffice when indexes are declared inline; the isolated
    # regression identity deliberately has no separate INDEX/ALTER privilege.
    ddl = str(CreateTable(table).compile(engine))
    at = ddl.rfind(")")
    inline = [f"INDEX {index.name} ({','.join(c.name for c in index.columns)})" for index in table.indexes]
    ddl = ddl[:at] + ", " + ", ".join(inline) + ddl[at:]
    with engine.begin() as connection:
        connection.exec_driver_sql(ddl)
    now = datetime(2026, 1, 1, 12)
    rows = [dict(id=f"old-{i:06}", instance_id="target", source=f"source-{i%40}",
        source_mode="live", observed_at=now-timedelta(days=1,seconds=i), payload={"text":"x"*100}) for i in range(4000)]
    rows += [dict(id=f"live-{i:06}", instance_id="target", source=f"source-{i%40}",
        source_mode="live", observed_at=now-timedelta(seconds=i), payload={"value":i}) for i in range(80)]
    rows += [dict(id=f"replay-{i:06}", instance_id="target", source="other", source_mode="replay",
        observed_at=now+timedelta(seconds=i), payload={"value":999}) for i in range(80)]
    rows += [dict(id=f"foreign-{i:06}", instance_id="other", source="other", source_mode="live",
        observed_at=now+timedelta(seconds=i), payload={"value":999}) for i in range(80)]
    query = (f"SELECT * FROM {table.name} WHERE instance_id='target' AND source_mode='live' "
        "AND observed_at>='2026-01-01 11:55:00' ORDER BY observed_at DESC LIMIT 50")
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)
    with engine.connect() as connection:
        plan = json.loads(connection.execute(text("EXPLAIN FORMAT=JSON " + query)).scalar())
        selected = connection.execute(text(query)).mappings().all()
    assert [r["id"] for r in selected] == [f"live-{i:06}" for i in range(50)]
    ordering = plan["query_block"]["ordering_operation"]
    assert ordering["using_filesort"] is False, "short live window sorted retained per-instance history"
    assert ordering["table"]["key"] == "ix_evidence_instance_mode_time"
