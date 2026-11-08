"""Versioned human review with inherited, whole-source instance scope."""

from datetime import timedelta
import json
from sqlalchemy import select, exists, func, and_, or_
from . import models as m
from .access import authorize, current_permissions
from .audit import append_audit
from .common import PlatformError, fields, page_query
from .db import utcnow
from .events import get_event, event_instances, source_manifest_ids
from .evidence import create_manifest


def _versions(db, entry_id):
    return list(
        db.scalars(
            select(m.KnowledgeVersion)
            .where(m.KnowledgeVersion.entry_id == entry_id)
            .order_by(m.KnowledgeVersion.version)
        )
    )


def _entry_scope(db, entry_id):
    return set(
        db.scalars(
            select(m.KnowledgeResource.instance_id)
            .join(m.KnowledgeVersion, m.KnowledgeVersion.id == m.KnowledgeResource.version_id)
            .where(m.KnowledgeVersion.entry_id == entry_id)
        )
    )


def _author_scope(actor):
    permissions = current_permissions(actor)
    ids = set()
    for action in ("knowledge.create", "knowledge.review"):
        if action in permissions:
            ids.update(authorize(actor, action).instance_ids)
    return ids


def _get(actor, entry_id, action="knowledge.read", lock=False):
    query = select(m.KnowledgeEntry).where(m.KnowledgeEntry.id == entry_id)
    if lock:
        query = query.with_for_update()
    row = actor.db.scalar(query.execution_options(populate_existing=True))
    if not row:
        raise PlatformError(404, "not_found", "知识不存在。")
    ids = _entry_scope(actor.db, row.id)
    if not ids:
        raise PlatformError(404, "not_found", "知识不存在。")
    authorize(actor, "data.read", ids)
    authorize(actor, "knowledge.read", ids)
    if action != "knowledge.read":
        authorize(actor, action, ids)
    return row


def _sources(actor, body, base=None):
    db = actor.db
    ids = set(body.get("instance_ids", []))
    refs = dict((base or {}).get("source_refs") or {})
    manifests = set((base or {}).get("source_manifest_ids") or [])
    for key in ("source_event_id", "verification_id", "source_investigation_ids"):
        if body.get(key):
            refs[key] = body[key]
    if refs.get("source_event_id"):
        event = get_event(actor, refs["source_event_id"])
        ids.update(event_instances(db, event))
        manifests.update(source_manifest_ids(db, event))
        if event.manifest_id:
            manifests.add(event.manifest_id)
    if refs.get("verification_id"):
        verification = db.get(m.Verification, refs["verification_id"])
        if not verification:
            raise PlatformError(404, "not_found", "来源复验不存在。")
        event = get_event(actor, verification.event_id)
        ids.update(event_instances(db, event))
        ids.add(verification.instance_id)
        if verification.manifest_id:
            manifests.add(verification.manifest_id)
    investigation_ids = refs.get("source_investigation_ids", [])
    if not isinstance(investigation_ids, list) or len(investigation_ids) > 5:
        raise PlatformError(422, "invalid_sources", "来源调查最多5项。")
    for identifier in investigation_ids:
        investigation = db.get(m.Investigation, identifier)
        if not investigation:
            raise PlatformError(404, "not_found", "来源调查不存在。")
        resources = set(
            db.scalars(
                select(m.InvestigationResource.instance_id).where(
                    m.InvestigationResource.investigation_id == identifier
                )
            )
        )
        authorize(actor, "data.read", resources)
        ids.update(resources)
        if investigation.manifest_id:
            manifests.add(investigation.manifest_id)
    for identifier in manifests:
        manifest = db.get(m.EvidenceManifest, identifier)
        if not manifest:
            raise PlatformError(404, "not_found", "来源证据清单不存在。")
        ids.update(manifest.instance_ids)
    if not ids or len(ids) > 5:
        raise PlatformError(422, "invalid_scope", "知识需要1–5个来源实例。")
    authorize(actor, "data.read", ids)
    authorize(actor, "knowledge.create", ids)
    return ids, refs, sorted(manifests)


def _save_version(actor, entry, body, number, prior=None):
    db = actor.db
    base = (prior.content or {}) if prior else {}
    inherited = dict(body)
    inherited["instance_ids"] = sorted(
        set(body.get("instance_ids", [])) | _entry_scope(db, entry.id)
    )
    ids, refs, manifests = _sources(actor, inherited, base)
    ids.update(_entry_scope(db, entry.id))  # new versions never shrink inherited scope
    authorize(actor, "data.read", ids)
    authorize(actor, "knowledge.create", ids)
    if len(ids) > 5:
        raise PlatformError(422, "invalid_scope", "知识来源实例超过5个。")
    content = dict(body.get("content") or {})
    content.update(
        source_refs=refs, source_manifest_ids=manifests, source="human_draft", review_required=True
    )
    version = m.KnowledgeVersion(
        entry_id=entry.id,
        version=number,
        created_by=actor.user_id,
        engine=body.get("engine", "mysql"),
        engine_versions=body.get("engine_versions", []),
        tags=body.get("tags", []),
        symptoms=body.get("symptoms", []),
        content=content,
        status="draft",
        source_event_id=refs.get("source_event_id"),
        verification_id=refs.get("verification_id"),
    )
    db.add(version)
    db.flush()
    for identifier in ids:
        db.add(m.KnowledgeResource(version_id=version.id, instance_id=identifier))
    # Knowledge inherits and extends source protection even if ordinary evidence expires.
    starts = []
    ends = []
    for identifier in manifests:
        source = db.get(m.EvidenceManifest, identifier)
        source.retain_until = max(source.retain_until, utcnow() + timedelta(days=90))
        starts.append(source.window_start)
        ends.append(source.window_end)
        for pin in db.scalars(select(m.EvidencePin).where(m.EvidencePin.manifest_id == identifier)):
            pin.retain_until = source.retain_until
    if starts:
        manifest = create_manifest(db, "knowledge", version.id, ids, min(starts), max(ends))
        version.content = dict(version.content, manifest_id=manifest.id)
    entry.status = "draft"
    db.flush()
    append_audit(
        db,
        actor.user_id,
        "knowledge.create_version",
        "knowledge",
        entry.id,
        details={"version": number, "instance_ids": sorted(ids)},
    )
    return version


def create_entry(actor, body):
    title = str(body.get("title", "")).strip()
    if not title or len(title) > 255:
        raise PlatformError(422, "invalid_title", "需要1–255字标题。")
    # Validate sources before adding an entry, so a denial has no partial object.
    _sources(actor, body)
    entry = m.KnowledgeEntry(title=title, created_by=actor.user_id, status="draft")
    actor.db.add(entry)
    actor.db.flush()
    _save_version(actor, entry, body, 1)
    return get_entry(actor, entry.id)


def get_entry(actor, entry_id):
    entry = _get(actor, entry_id)
    versions = _versions(actor.db, entry.id)
    latest = versions[-1]
    author = _entry_scope(actor.db, entry.id).issubset(_author_scope(actor))
    if not author:
        # Readers consume the explicitly published version, including while a
        # newer draft is being edited. Draft bodies/tags never become public
        # merely because the entry had an older approved version.
        versions = [
            v for v in versions if v.status in ("published", "superseded") and v.reviewer_id
        ]
        latest = next((v for v in versions if v.version == entry.approved_version), None)
        if latest is None:
            raise PlatformError(404, "not_found", "知识不存在。")
    return dict(
        fields(
            entry,
            ["id", "title", "status", "created_by", "created_at", "updated_at", "approved_version"],
        ),
        **({"status": "published"} if not author else {}),
        version=latest.version,
        instance_ids=sorted(_entry_scope(actor.db, entry.id)),
        content=latest.content,
        tags=latest.tags,
        engine=latest.engine,
        engine_versions=latest.engine_versions,
        versions=[
            fields(
                row,
                [
                    "id",
                    "version",
                    "status",
                    "engine",
                    "engine_versions",
                    "tags",
                    "symptoms",
                    "content",
                    "source_event_id",
                    "verification_id",
                    "created_by",
                    "reviewer_id",
                    "reviewed_at",
                    "review_note",
                    "created_at",
                ],
            )
            for row in versions
        ],
    )


def add_version(actor, entry_id, body):
    entry = _get(actor, entry_id, "knowledge.create", lock=True)
    versions = _versions(actor.db, entry.id)
    prior = versions[-1]
    if body.get("version") != prior.version:
        raise PlatformError(409, "version_conflict", "知识已有新版本。")
    _save_version(actor, entry, body, prior.version + 1, prior)
    return get_entry(actor, entry.id)


def submit_review(actor, entry_id, body):
    entry = _get(actor, entry_id, "knowledge.create", lock=True)
    version = _versions(actor.db, entry.id)[-1]
    if body.get("version") != version.version:
        raise PlatformError(409, "version_conflict", "知识已有新版本。")
    if version.status not in ("draft", "rejected"):
        raise PlatformError(409, "invalid_state", "此版本不能提交审核。")
    version.status = "in_review"
    entry.status = "in_review"
    append_audit(
        actor.db,
        actor.user_id,
        "knowledge.submit_review",
        "knowledge",
        entry.id,
        details={"version": version.version},
    )
    return get_entry(actor, entry.id)


def review_entry(actor, entry_id, body):
    entry = _get(actor, entry_id, "knowledge.review", lock=True)
    versions = _versions(actor.db, entry.id)
    version = versions[-1]
    if body.get("version") != version.version:
        raise PlatformError(409, "version_conflict", "知识已有新版本。")
    decision = body.get("decision")
    note = str(body.get("note", body.get("reason", ""))).strip()
    if decision not in ("published", "approved", "rejected", "withdrawn") or not note:
        raise PlatformError(422, "invalid_review", "需要审核结论和说明。")
    if decision == "withdrawn":
        if version.status != "published":
            raise PlatformError(409, "invalid_state", "只能撤回已发布版本。")
        version.status = "superseded"
        entry.status = "superseded"
        entry.approved_version = None
    else:
        if version.status != "in_review":
            raise PlatformError(409, "invalid_state", "只有待审核版本能审核。")
        version.reviewer_id = actor.user_id
        version.reviewed_at = utcnow()
        version.review_note = note
        version.status = decision
        entry.status = decision
        if decision == "published":
            for older in versions[:-1]:
                if older.status == "published":
                    older.status = "superseded"
            entry.approved_version = version.version
    append_audit(
        actor.db,
        actor.user_id,
        "knowledge." + decision,
        "knowledge",
        entry.id,
        details={"version": version.version, "decision": decision},
    )
    return get_entry(actor, entry.id)


def publish_entry(actor, entry_id, body):
    entry = _get(actor, entry_id, "knowledge.review", lock=True)
    versions = _versions(actor.db, entry.id)
    version = versions[-1]
    if body.get("version") != version.version:
        raise PlatformError(409, "version_conflict", "知识已有新版本。")
    if version.status != "approved" or not version.reviewer_id:
        raise PlatformError(409, "review_required", "需要人工审核后发布。")
    for older in versions[:-1]:
        if older.status == "published":
            older.status = "superseded"
    version.status = "published"
    entry.status = "published"
    entry.approved_version = version.version
    append_audit(
        actor.db,
        actor.user_id,
        "knowledge.published",
        "knowledge",
        entry.id,
        details={"version": version.version},
    )
    return get_entry(actor, entry.id)


def list_entries(actor, *, limit=50, cursor=None, q=None, engine=None, tag=None, status=None):
    ids = (
        authorize(actor, "knowledge.read").instance_ids & authorize(actor, "data.read").instance_ids
    )
    resources = (
        select(m.KnowledgeResource.version_id)
        .join(m.KnowledgeVersion, m.KnowledgeVersion.id == m.KnowledgeResource.version_id)
        .where(m.KnowledgeVersion.entry_id == m.KnowledgeEntry.id)
    )
    author = ~exists(resources.where(m.KnowledgeResource.instance_id.notin_(_author_scope(actor))))
    published = m.KnowledgeEntry.approved_version.is_not(None)
    query = select(m.KnowledgeEntry).where(
        exists(resources),
        ~exists(resources.where(m.KnowledgeResource.instance_id.notin_(ids))),
        or_(author, published),
    )
    if q:
        if len(q) > 200:
            raise PlatformError(422, "invalid_search", "关键词最多200字。")
        query = query.where(m.KnowledgeEntry.title.contains(q, autoescape=True))
    if status:
        query = query.where(
            or_(and_(author, m.KnowledgeEntry.status == status), and_(~author, published))
            if status == "published"
            else and_(author, m.KnowledgeEntry.status == status)
        )
    visible_version = or_(author, m.KnowledgeVersion.version == m.KnowledgeEntry.approved_version)
    if engine:
        query = query.where(
            exists(
                select(m.KnowledgeVersion.id).where(
                    m.KnowledgeVersion.entry_id == m.KnowledgeEntry.id,
                    visible_version,
                    m.KnowledgeVersion.engine == engine,
                )
            )
        )
    # Tag filtering stays scoped before count; use the JSON comparator on supported engines.
    if tag:
        if actor.db.bind.dialect.name == "mysql":
            condition = func.JSON_CONTAINS(m.KnowledgeVersion.tags, json.dumps(tag)) == 1
        else:
            values = func.json_each(m.KnowledgeVersion.tags).table_valued("value")
            condition = exists(select(values.c.value).where(values.c.value == tag))
        query = query.where(
            exists(
                select(m.KnowledgeVersion.id).where(
                    m.KnowledgeVersion.entry_id == m.KnowledgeEntry.id, visible_version, condition
                )
            )
        )
    rows, total, next_cursor = page_query(
        actor.db, query, m.KnowledgeEntry, limit=limit, cursor=cursor
    )
    return {
        "items": [get_entry(actor, row.id) for row in rows],
        "total": total,
        "next_cursor": next_cursor,
    }
