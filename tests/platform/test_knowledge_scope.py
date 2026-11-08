import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.platform.test_events import db, graph, manual_event
from platform_app import models as m
from platform_app.knowledge import (
    create_entry,
    get_entry,
    list_entries,
    add_version,
    review_entry,
    submit_review,
    publish_entry,
)


def test_knowledge_inherits_all_sources_and_scopes_counts(db):
    actor, a, b, grant = graph(db)
    grant.scope_type = "global"
    grant.scope_id = "*"
    db.flush()
    event = manual_event(db, b)
    result = create_entry(
        actor,
        {
            "title": "cross-source",
            "instance_ids": [a.id],
            "source_event_id": event.id,
            "content": {"body": "reviewed facts"},
        },
    )
    assert set(result["instance_ids"]) == {a.id, b.id}
    grant.scope_type = "instance"
    grant.scope_id = a.id
    db.flush()
    with pytest.raises(HTTPException):
        get_entry(actor, result["id"])
    assert list_entries(actor)["total"] == 0


def test_new_version_cannot_shrink_original_scope(db):
    actor, a, b, grant = graph(db)
    grant.scope_type = "global"
    grant.scope_id = "*"
    db.flush()
    result = create_entry(
        actor, {"title": "all sources", "instance_ids": [a.id, b.id], "content": {"body": "facts"}}
    )
    changed = add_version(
        actor, result["id"], {"version": 1, "instance_ids": [a.id], "content": {"body": "updated"}}
    )
    assert set(changed["instance_ids"]) == {a.id, b.id}
    assert changed["version"] == 2
    inherited = add_version(
        actor, result["id"], {"version": 2, "content": {"body": "inherited scope"}}
    )
    assert set(inherited["instance_ids"]) == {a.id, b.id}


def test_tag_search_is_exact_inside_multiple_tags(db):
    actor, a, _, _ = graph(db)
    create_entry(
        actor,
        {
            "title": "indexed knowledge",
            "instance_ids": [a.id],
            "content": {"body": "facts"},
            "tags": ["mysql", "locking"],
        },
    )
    assert list_entries(actor, tag="mysql")["total"] == 1
    assert list_entries(actor, tag="sql")["total"] == 0


def test_model_draft_cannot_publish_without_human_review(db):
    actor, a, _, grant = graph(db)
    result = create_entry(
        actor,
        {
            "title": "draft",
            "instance_ids": [a.id],
            "content": {"body": "hypothesis", "source": "live_model"},
        },
    )
    submit_review(actor, result["id"], {"version": 1})
    with pytest.raises(HTTPException):
        review_entry(
            actor, result["id"], {"version": 1, "decision": "published", "note": "reviewed"}
        )
    reviewrole = m.Role(
        name="knowledge_reviewer", permissions=["knowledge.review", "knowledge.read", "data.read"]
    )
    db.add(reviewrole)
    db.flush()
    db.add(
        m.UserGrant(
            user_id=actor.user_id, role_id=reviewrole.id, scope_type="instance", scope_id=a.id
        )
    )
    db.flush()
    published = review_entry(
        actor,
        result["id"],
        {"version": 1, "decision": "published", "note": "human reviewed applicability"},
    )
    assert published["status"] == "published" and published["approved_version"] == 1
    assert published["versions"][0]["reviewer_id"] == actor.user_id


def test_approved_publish_supersedes_previous_published_version(db):
    actor, a, _, _ = graph(db)
    role = m.Role(
        name="knowledge_reviewer", permissions=["knowledge.review", "knowledge.read", "data.read"]
    )
    db.add(role)
    db.flush()
    db.add(
        m.UserGrant(user_id=actor.user_id, role_id=role.id, scope_type="instance", scope_id=a.id)
    )
    db.flush()
    row = create_entry(
        actor, {"title": "versioned procedure", "instance_ids": [a.id], "content": {"body": "v1"}}
    )
    for version in (1, 2):
        if version == 2:
            add_version(actor, row["id"], {"version": 1, "content": {"body": "corrected v2"}})
        submit_review(actor, row["id"], {"version": version})
        review_entry(
            actor,
            row["id"],
            {"version": version, "decision": "approved", "note": "reviewed this version"},
        )
        row = publish_entry(actor, row["id"], {"version": version})
    assert row["approved_version"] == 2
    assert [v["status"] for v in row["versions"]] == ["superseded", "published"]


def test_read_only_user_sees_approved_version_without_draft_or_tag_leak(db):
    actor, a, _, grant = graph(db)
    role = db.get(m.Role, grant.role_id)
    author_permissions = list(role.permissions) + ["knowledge.review"]
    role.permissions = author_permissions
    db.flush()
    row = create_entry(
        actor,
        {
            "title": "reviewed procedure",
            "instance_ids": [a.id],
            "tags": ["public-tag"],
            "content": {"body": "approved body"},
        },
    )
    role.permissions = ["data.read", "knowledge.read"]
    db.flush()
    with pytest.raises(HTTPException):
        get_entry(actor, row["id"])
    assert list_entries(actor)["total"] == 0
    role.permissions = author_permissions
    db.flush()
    submit_review(actor, row["id"], {"version": 1})
    review_entry(actor, row["id"], {"version": 1, "decision": "approved", "note": "reviewed"})
    publish_entry(actor, row["id"], {"version": 1})
    add_version(
        actor,
        row["id"],
        {"version": 1, "tags": ["private-draft-tag"], "content": {"body": "unreviewed body"}},
    )
    role.permissions = ["data.read", "knowledge.read"]
    db.flush()
    visible = get_entry(actor, row["id"])
    assert visible["version"] == 1 and visible["status"] == "published"
    assert visible["content"]["body"] == "approved body" and len(visible["versions"]) == 1
    assert list_entries(actor, status="published")["total"] == 1
    assert list_entries(actor, tag="private-draft-tag")["total"] == 0
