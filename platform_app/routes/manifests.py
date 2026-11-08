"""Bounded provenance metadata, authorized through its owning business object."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, or_
from .. import models as m
from ..access import authorize
from ..common import PlatformError, fields
from ..identity import current_actor
from ..events import get_event
from ..evidence import get_manifest

router = APIRouter(tags=["evidence manifests"])


def _owner(actor, manifest):
    db = actor.db
    kind = manifest.owner_type
    identifier = manifest.owner_id
    if kind == "event":
        get_event(actor, identifier)
    elif kind == "investigation":
        from ..investigations import get_investigation

        get_investigation(actor, identifier)
    elif kind == "knowledge":
        from ..knowledge import get_entry

        version = db.get(m.KnowledgeVersion, identifier)
        if not version:
            raise PlatformError(404, "not_found", "来源不存在。")
        visible = get_entry(actor, version.entry_id)
        if version.id not in {item["id"] for item in visible["versions"]}:
            raise PlatformError(404, "not_found", "来源不存在。")
    elif kind in ("remediation", "verification"):
        owner = db.get(m.RemediationPlan if kind == "remediation" else m.Verification, identifier)
        if not owner:
            raise PlatformError(404, "not_found", "来源不存在。")
        get_event(actor, owner.event_id)
    elif kind == "alert":
        owner = db.get(m.AlertOccurrence, identifier)
        if not owner:
            raise PlatformError(404, "not_found", "来源不存在。")
        authorize(actor, "data.read", [owner.instance_id])
        if owner.instance_id not in manifest.instance_ids:
            raise PlatformError(404, "not_found", "来源不存在。")
        authorize(actor, "event.read", manifest.instance_ids)
    else:
        raise PlatformError(404, "not_found", "来源不存在。")


def _validate_material(actor, manifest, visited=None, depth=0):
    """Fail closed on imported/inconsistent provenance before counts or pages."""
    visited = set() if visited is None else visited
    if manifest.id in visited:
        return
    if depth > 16 or len(visited) >= 128:
        raise PlatformError(409, "manifest_graph_limit", "来源图超过受限核验预算。")
    visited.add(manifest.id)
    _owner(actor, manifest)
    ids = manifest.instance_ids
    db = actor.db
    items = select(m.ManifestItem).where(m.ManifestItem.manifest_id == manifest.id)
    invalid = db.scalar(
        items.with_only_columns(m.ManifestItem.id)
        .where(m.ManifestItem.instance_id.notin_(ids))
        .limit(1)
    )
    evidence = items.join(m.Evidence, m.ManifestItem.evidence_id == m.Evidence.id).join(
        m.CollectionRun, m.Evidence.run_id == m.CollectionRun.id
    )
    invalid_evidence = db.scalar(
        evidence.with_only_columns(m.ManifestItem.id)
        .where(
            or_(
                m.Evidence.instance_id.notin_(ids),
                m.Evidence.instance_id != m.ManifestItem.instance_id,
                m.CollectionRun.instance_id != m.Evidence.instance_id,
            )
        )
        .limit(1)
    )
    frames = items.join(m.MetricFrame, m.ManifestItem.metric_run_id == m.MetricFrame.run_id).join(
        m.CollectionRun, m.MetricFrame.run_id == m.CollectionRun.id
    )
    invalid_frame = db.scalar(
        frames.with_only_columns(m.ManifestItem.id)
        .where(
            or_(
                m.MetricFrame.instance_id.notin_(ids),
                m.MetricFrame.instance_id != m.ManifestItem.instance_id,
                m.CollectionRun.instance_id != m.MetricFrame.instance_id,
            )
        )
        .limit(1)
    )
    if invalid or invalid_evidence or invalid_frame:
        raise PlatformError(404, "not_found", "来源不存在。")
    references = list(
        db.scalars(items.where(m.ManifestItem.item_type == "manifest_reference").limit(129))
    )
    if len(references) > 128:
        raise PlatformError(409, "manifest_graph_limit", "来源图超过受限核验预算。")
    for item in references:
        identifier = (item.payload or {}).get("manifest_id")
        if not isinstance(identifier, str):
            raise PlatformError(404, "not_found", "来源不存在。")
        source = get_manifest(actor, identifier)
        if not set(source.instance_ids).issubset(ids):
            raise PlatformError(404, "not_found", "来源不存在。")
        _validate_material(actor, source, visited, depth + 1)


@router.get("/evidence-manifests/{identifier}")
def manifest_detail(
    identifier: str, limit: int = 50, cursor: str | None = None, actor=Depends(current_actor)
):
    manifest = get_manifest(actor, identifier)
    _validate_material(actor, manifest)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise PlatformError(422, "invalid_page", "每页1–100项。")
    if cursor is not None and (not cursor.isdigit() or len(cursor) > 7):
        raise PlatformError(422, "invalid_cursor", "分页标记无效。")
    offset = int(cursor or "0")
    query = select(m.ManifestItem).where(m.ManifestItem.manifest_id == identifier)
    total = actor.db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(actor.db.scalars(query.order_by(m.ManifestItem.id).offset(offset).limit(limit)))
    coverage = actor.db.scalar(query.where(m.ManifestItem.item_type == "coverage").limit(1))
    safe_coverage = (
        {
            k: (coverage.payload or {}).get(k)
            for k in ("coverage_complete", "truncated", "gaps", "prometheus_materialization")
        }
        if coverage
        else {"coverage_complete": False, "gaps": ["coverage_record_missing"]}
    )
    items = []
    for row in rows:
        if row.instance_id not in manifest.instance_ids:
            raise PlatformError(404, "not_found", "来源不存在。")
        value = fields(
            row,
            ["id", "instance_id", "item_type", "evidence_id", "metric_run_id", "payload_sha256"],
        )
        payload = row.payload or {}
        if row.item_type == "evidence":
            value.update({key: payload[key] for key in ("source", "observed_at") if key in payload})
        if row.item_type == "manifest_reference" and payload.get("manifest_id"):
            value["reference_manifest_id"] = payload["manifest_id"]
        items.append(value)
    return {
        **fields(
            manifest,
            [
                "id",
                "owner_type",
                "owner_id",
                "version",
                "instance_ids",
                "window_start",
                "window_end",
                "sources",
                "manifest_sha256",
                "retain_until",
            ],
        ),
        "coverage": safe_coverage,
        "items": items,
        "total": total,
        "next_cursor": str(offset + limit) if offset + limit < total else None,
    }
