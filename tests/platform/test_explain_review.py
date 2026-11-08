"""Independent EXPLAIN failure-path review; no target connection is opened."""

from contextlib import contextmanager
import json
from sqlalchemy import select, func
from pymysql.err import OperationalError
from tests.platform.test_explain import explain_graph, worker
from platform_app import models as m


def test_driver_original_text_never_reaches_task_evidence_audit_or_worker_log(
    explain_graph, monkeypatch, caplog
):
    from platform_app import explain, worker as orchestration

    g = explain_graph
    queued, lease, _, _ = worker(g, monkeypatch)
    marker = "review-private-bound-literal-and-driver-path"

    @contextmanager
    def rejected(*args, **kwargs):
        raise OperationalError(1064, marker)
        yield

    monkeypatch.setattr(explain, "open_mysql", rejected)
    result = orchestration.execute(g.factory, lease, g.config)
    g.db.expire_all()
    task = g.db.get(m.Task, queued.id)
    assert task.status == "failed"
    assert marker not in json.dumps(
        {
            "result": result,
            "task": task.result,
            "code": task.error_code,
            "message": task.error_message,
        }
    )
    assert marker not in caplog.text
    assert marker not in "".join(
        json.dumps(row.details) for row in g.db.scalars(select(m.AuditLog))
    )
    assert (
        g.db.scalar(
            select(func.count())
            .select_from(m.Evidence)
            .where(m.Evidence.source == "mysql.explain_plan")
        )
        == 0
    )
