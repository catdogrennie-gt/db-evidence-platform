"""Independent publication/provenance boundary probes; no target MySQL writes."""

from datetime import timedelta
from types import SimpleNamespace
from dataclasses import replace
import os

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.dialects import mysql

from tests.platform.test_events import db, graph, manual_event, observe
from platform_app import models as m
from platform_app.access import actor_for_user, current_permissions
from platform_app.config import load_config
from platform_app.db import create_db_engine, create_session_factory, utcnow
from platform_app.evidence import create_manifest
from platform_app.knowledge import (
    create_entry,
    get_entry,
    list_entries,
    add_version,
    submit_review,
    review_entry,
    publish_entry,
)
from platform_app.routes.manifests import manifest_detail


def setup_knowledge(db):
    author, a, b, grant = graph(db)
    role = db.get(m.Role, grant.role_id)
    role.permissions = list(role.permissions) + ["knowledge.review"]
    reader = m.User(username="publication-reader", password_hash="not-used")
    reader_role = m.Role(
        name="publication-reader", permissions=["data.read", "knowledge.read", "event.read"]
    )
    db.add_all([reader, reader_role])
    db.flush()
    reader_grant = m.UserGrant(
        user_id=reader.id, role_id=reader_role.id, scope_type="instance", scope_id=a.id
    )
    db.add(reader_grant)
    db.flush()
    return SimpleNamespace(
        author=author,
        reader=actor_for_user(db, reader.id),
        a=a,
        b=b,
        grant=grant,
        reader_role=reader_role,
        reader_grant=reader_grant,
    )


def draft(g, title="published-reference", **extra):
    return create_entry(
        g.author,
        {
            "title": title,
            "instance_ids": [g.a.id],
            "engine": "mysql",
            "tags": ["reviewed-public-tag"],
            "content": {"body": "reviewed-public-body"},
            **extra,
        },
    )


def publish(g, row, number=1):
    submit_review(g.author, row["id"], {"version": number})
    return review_entry(
        g.author,
        row["id"],
        {"version": number, "decision": "published", "note": "Human verified this version"},
    )


@pytest.mark.parametrize(
    "filter_",
    [
        {},
        {"q": "unpublished-private-title"},
        {"tag": "private-tag"},
        {"engine": "unreviewed-engine"},
        {"status": "draft"},
        {"status": "published"},
    ],
)
def test_reader_cannot_discover_draft_by_detail_count_page_or_filter(db, filter_):
    g = setup_knowledge(db)
    row = draft(
        g, title="unpublished-private-title", engine="unreviewed-engine", tags=["private-tag"]
    )
    with pytest.raises(HTTPException) as denied:
        get_entry(g.reader, row["id"])
    assert denied.value.status_code == 404
    result = list_entries(g.reader, limit=1, **filter_)
    assert result == {"items": [], "total": 0, "next_cursor": None}
    assert get_entry(g.author, row["id"])["content"]["body"] == "reviewed-public-body"


@pytest.mark.parametrize("new_status", ["draft", "in_review", "approved", "rejected"])
def test_reader_filters_and_body_are_bound_to_published_version_not_new_work(db, new_status):
    g = setup_knowledge(db)
    row = publish(g, draft(g))
    add_version(
        g.author,
        row["id"],
        {
            "version": 1,
            "engine": "private-draft-engine",
            "tags": ["private-draft-tag"],
            "content": {"body": "private-unreviewed-marker"},
        },
    )
    if new_status != "draft":
        submit_review(g.author, row["id"], {"version": 2})
        if new_status != "in_review":
            review_entry(
                g.author,
                row["id"],
                {"version": 2, "decision": new_status, "note": "review transition only"},
            )
    result = get_entry(g.reader, row["id"])
    assert result["version"] == result["approved_version"] == 1 and result["status"] == "published"
    assert result["engine"] == "mysql" and result["tags"] == ["reviewed-public-tag"]
    assert [v["version"] for v in result["versions"]] == [1]
    assert "private-unreviewed-marker" not in str(result)
    for filter_ in (
        {"tag": "private-draft-tag"},
        {"engine": "private-draft-engine"},
        {"status": new_status},
    ):
        assert list_entries(g.reader, **filter_)["total"] == 0
    assert (
        list_entries(g.reader, engine="mysql", tag="reviewed-public-tag", status="published")[
            "total"
        ]
        == 1
    )


def test_approved_but_not_published_never_becomes_reader_visible(db):
    g = setup_knowledge(db)
    row = draft(g)
    submit_review(g.author, row["id"], {"version": 1})
    review_entry(
        g.author,
        row["id"],
        {"version": 1, "decision": "approved", "note": "Still requires explicit publication"},
    )
    with pytest.raises(HTTPException):
        get_entry(g.reader, row["id"])
    assert list_entries(g.reader)["total"] == 0


@pytest.mark.parametrize("mode", ["direct", "approved_then_publish"])
def test_publishing_next_version_supersedes_prior_and_withdrawal_hides_entry(db, mode):
    g = setup_knowledge(db)
    row = publish(g, draft(g))
    add_version(g.author, row["id"], {"version": 1, "content": {"body": "reviewed second version"}})
    if mode == "direct":
        publish(g, row, 2)
    else:
        submit_review(g.author, row["id"], {"version": 2})
        review_entry(
            g.author, row["id"], {"version": 2, "decision": "approved", "note": "Human review"}
        )
        publish_entry(g.author, row["id"], {"version": 2})
    visible = get_entry(g.reader, row["id"])
    assert visible["version"] == 2 and [item["status"] for item in visible["versions"]] == [
        "superseded",
        "published",
    ]
    review_entry(
        g.author,
        row["id"],
        {"version": 2, "decision": "withdrawn", "note": "Applicability no longer holds"},
    )
    with pytest.raises(HTTPException):
        get_entry(g.reader, row["id"])
    assert list_entries(g.reader, status="published")["total"] == 0


def test_author_permission_on_foreign_instance_does_not_reveal_local_draft(db):
    g = setup_knowledge(db)
    row = draft(g)
    role = m.Role(name="foreign-author", permissions=["knowledge.create", "knowledge.review"])
    db.add(role)
    db.flush()
    db.add(
        m.UserGrant(
            user_id=g.reader.user_id, role_id=role.id, scope_type="instance", scope_id=g.b.id
        )
    )
    db.flush()
    with pytest.raises(HTTPException):
        get_entry(g.reader, row["id"])
    assert list_entries(g.reader)["total"] == 0


@pytest.mark.parametrize("nested", [False, True])
def test_reader_cannot_open_new_draft_version_manifest_through_older_public_entry(db, nested):
    g = setup_knowledge(db)
    row = publish(g, draft(g))
    add_version(g.author, row["id"], {"version": 1, "content": {"body": "not published"}})
    version = db.scalar(
        select(m.KnowledgeVersion).where(
            m.KnowledgeVersion.entry_id == row["id"], m.KnowledgeVersion.version == 2
        )
    )
    now = utcnow()
    hidden = create_manifest(db, "knowledge", version.id, [g.a.id], now - timedelta(minutes=3), now)
    if nested:
        owner = manual_event(db, g.a)
        parent = create_manifest(db, "event", owner.id, [g.a.id], now - timedelta(minutes=3), now)
        db.add(
            m.ManifestItem(
                manifest_id=parent.id,
                instance_id=g.a.id,
                item_type="manifest_reference",
                payload={"manifest_id": hidden.id},
                payload_sha256="a" * 64,
            )
        )
        db.flush()
        identifier = parent.id
    else:
        identifier = hidden.id
    assert get_entry(g.reader, row["id"])["version"] == 1
    with pytest.raises(HTTPException) as denied:
        manifest_detail(identifier, limit=1, actor=g.reader)
    assert denied.value.status_code == 404


def test_manifest_rejects_reference_to_foreign_owner_even_when_declared_scope_is_local(db):
    g = setup_knowledge(db)
    now = utcnow()
    local = manual_event(db, g.a)
    foreign = manual_event(db, g.b)
    parent = create_manifest(db, "event", local.id, [g.a.id], now - timedelta(minutes=1), now)
    child = create_manifest(db, "event", foreign.id, [g.a.id], now - timedelta(minutes=1), now)
    db.add(
        m.ManifestItem(
            manifest_id=parent.id,
            instance_id=g.a.id,
            item_type="manifest_reference",
            payload={"manifest_id": child.id},
            payload_sha256="b" * 64,
        )
    )
    db.flush()
    with pytest.raises(HTTPException):
        manifest_detail(parent.id, limit=1, actor=g.reader)


def test_manifest_rejects_authoritative_collection_run_scope_mismatch(db):
    g = setup_knowledge(db)
    now = utcnow()
    owner = manual_event(db, g.a)
    manifest = create_manifest(db, "event", owner.id, [g.a.id], now - timedelta(minutes=1), now)
    foreign_run, _ = observe(db, g.b, now)
    evidence = m.Evidence(
        instance_id=g.a.id,
        run_id=foreign_run.id,
        source="mysql.identity",
        observed_at=now,
        server_uuid="test-only",
        payload={},
        payload_sha256="c" * 64,
    )
    db.add(evidence)
    db.flush()
    db.add(
        m.ManifestItem(
            manifest_id=manifest.id,
            instance_id=g.a.id,
            item_type="evidence",
            evidence_id=evidence.id,
            payload={},
            payload_sha256="d" * 64,
        )
    )
    db.flush()
    with pytest.raises(HTTPException):
        manifest_detail(manifest.id, limit=1, actor=g.reader)


@pytest.mark.parametrize("shape", ["depth", "width"])
def test_manifest_graph_budget_denies_before_returning_a_partial_first_page(db, shape):
    g = setup_knowledge(db)
    now = utcnow()
    owner = manual_event(db, g.a)
    manifests = [
        create_manifest(db, "event", owner.id, [g.a.id], now - timedelta(minutes=1), now)
        for index in range(19 if shape == "depth" else 130)
    ]
    for index, child in enumerate(manifests[1:]):
        parent = manifests[index] if shape == "depth" else manifests[0]
        db.add(
            m.ManifestItem(
                manifest_id=parent.id,
                instance_id=g.a.id,
                item_type="manifest_reference",
                payload={"manifest_id": child.id},
                payload_sha256="e" * 64,
            )
        )
    db.flush()
    with pytest.raises(HTTPException) as denied:
        manifest_detail(manifests[0].id, limit=1, actor=g.reader)
    assert denied.value.status_code == 409


def test_mysql_compilation_keeps_tag_and_engine_correlated_to_approved_version(db, monkeypatch):
    import platform_app.knowledge as knowledge

    g = setup_knowledge(db)
    publish(g, draft(g))

    class DBProxy:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        def __getattr__(self, key):
            return getattr(db, key)

    class Captured(Exception):
        pass

    compiled = []

    def capture(db_, statement, model, **kwargs):
        compiled.append(str(statement.compile(dialect=mysql.dialect())))
        raise Captured()

    monkeypatch.setattr(knowledge, "page_query", capture)
    with pytest.raises(Captured):
        list_entries(
            replace(g.reader, db=DBProxy()),
            engine="mysql",
            tag="private-draft-tag",
            status="published",
        )
    sql = compiled[0]
    assert (
        "JSON_CONTAINS" in sql
        and "knowledge_versions.version = knowledge_entries.approved_version" in sql
    )
    assert (
        "knowledge_resources.instance_id NOT IN" in sql
        and "knowledge_entries.approved_version IS NOT NULL" in sql
    )
    assert "private-draft-tag" not in sql


@pytest.mark.skipif(
    not os.environ.get("DBA_PLATFORM_CONFIG"), reason="Private real MySQL configuration is opt-in"
)
def test_real_mysql_reader_query_compiles_executes_and_filters_without_writes():
    engine = create_db_engine(load_config())
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with create_session_factory(engine)() as session:
            users = list(
                session.scalars(
                    select(m.User.id)
                    .join(m.UserGrant, m.UserGrant.user_id == m.User.id)
                    .join(m.Role, m.Role.id == m.UserGrant.role_id)
                    .where(m.Role.name == "viewer", m.User.active.is_(True))
                    .distinct()
                    .limit(10)
                )
            )
            reader = None
            for user_id in users:
                candidate = actor_for_user(session, user_id)
                permissions = current_permissions(candidate)
                if "knowledge.read" in permissions and not permissions.intersection(
                    {"knowledge.create", "knowledge.review"}
                ):
                    reader = candidate
                    break
            if reader is None:
                pytest.skip("No configured pure knowledge reader exists")
            visible = list_entries(reader, limit=1, status="published", engine="mysql")
            assert all(
                item["status"] == "published" and item["version"] == item["approved_version"]
                for item in visible["items"]
            )
            assert list_entries(reader, limit=1, status="draft")["total"] == 0
            assert (
                list_entries(reader, limit=1, tag="__security_review_nonexistent_tag__")["total"]
                == 0
            )
            assert any("JSON_CONTAINS" in statement for statement in statements)
            assert not any(
                statement.lstrip()
                .upper()
                .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
                for statement in statements
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)
        engine.dispose()
