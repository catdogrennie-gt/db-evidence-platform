"""Allowlisted configuration projections; never credentials, dispatch or mutation.

Business data and configuration grants are intersected on every request. These
views do not reuse management helpers or relax authorization on existing APIs.
"""
from __future__ import annotations

import math
from fastapi import HTTPException
from sqlalchemy import or_, select

from . import models as m
from .access import authorize, current_permissions, current_roles
from .common import PlatformError, fields, iso, parse_time
from .db import utcnow
from .zabbix_items import safe_item, safe_text

READ_ACTIONS = frozenset({'data.read', 'event.read', 'knowledge.read', 'configuration.read',
                          'evidence.read_sensitive', 'audit.read', 'platform.read'})


def visible_ids(actor, **scope):
    ids = authorize(actor, 'configuration.read').instance_ids & authorize(actor, 'data.read').instance_ids
    identifier = scope.get('instance_id')
    if identifier and identifier not in ids:
        raise PlatformError(404, 'not_found', '对象不存在。')
    result = set()
    for row in actor.db.scalars(select(m.Instance).where(m.Instance.id.in_(ids))):
        system = actor.db.get(m.System, row.system_id)
        values = {'instance_id': row.id, 'system_id': system.id, 'department_id': system.department_id, 'environment': system.environment}
        if all(not value or values.get(key) == value for key, value in scope.items()):
            result.add(row.id)
    return result


def _mappings(actor, ids):
    return list(actor.db.scalars(select(m.MonitoringMapping).where(m.MonitoringMapping.instance_id.in_(ids), m.MonitoringMapping.layer == 'group')))


def source_summaries(actor, ids):
    mappings = _mappings(actor, ids)
    result = []
    for source in actor.db.scalars(select(m.MonitoringSource).where(m.MonitoringSource.id.in_({r.source_id for r in mappings})).order_by(m.MonitoringSource.name, m.MonitoringSource.id)):
        related = [r for r in mappings if r.source_id == source.id]
        result.append({**fields(source, ['id', 'system_id', 'last_success_at']),
                       'name': safe_text(source.name), 'provider': 'Zabbix', 'status': safe_text(source.status),
                       'has_credentials': bool(source.secret_ref), 'group_count': len(related),
                       'host_count': len({str(i.get('hostid')) for r in related for i in r.items}),
                       'item_count': len({(str(i.get('hostid')), str(i.get('itemid'))) for r in related for i in r.items})})
    return result


def selected_catalog(actor, source_id, body):
    ids = visible_ids(actor)
    mappings = [r for r in _mappings(actor, ids) if r.source_id == source_id]
    if not mappings:
        raise PlatformError(404, 'not_found', '来源不存在。')
    items = {}
    for mapping in mappings:
        for item in mapping.items:
            key = (str(item.get('hostid')), str(item.get('itemid')))
            items[key] = safe_item(item)
    search = body.get('search', '').casefold()
    selected = [item for _, item in sorted(items.items()) if not search or search in (' '.join(str(item.get(k, '')) for k in ('name', 'key', 'host_name'))).casefold()]
    offset, limit = body.get('offset', 0), body.get('limit', 50)
    return {'items': selected[offset:offset + limit], 'total': len(selected), 'offset': offset, 'limit': limit,
            'next_offset': offset + limit if offset + limit < len(selected) else None,
            'catalog_scope': 'selected_items', 'metadata_policy': 'bounded_vocabulary_no_raw_text_v1'}


def _tasks(actor, ids):
    from .routes.tasks import source_instances
    event_ids = authorize(actor, 'event.read').instance_ids if 'event.read' in current_permissions(actor) else frozenset()
    report_tasks = select(m.Investigation.task_id).join(m.InvestigationResource, m.InvestigationResource.investigation_id == m.Investigation.id).where(m.InvestigationResource.instance_id.in_(ids))
    candidates = actor.db.scalars(select(m.Task).where(or_(m.Task.instance_id.in_(ids), m.Task.id.in_(report_tasks))).order_by(m.Task.created_at.desc(), m.Task.id.desc()).limit(501))
    items = []
    for row in candidates:
        resources = source_instances(actor.db, row)
        # Reports can be linked by task_id before/without task.input_ref. Check
        # the full frozen scope instead of trusting just the task's first owner.
        for investigation in actor.db.scalars(select(m.Investigation).where(m.Investigation.task_id == row.id)):
            resources.update(actor.db.scalars(select(m.InvestigationResource.instance_id).where(m.InvestigationResource.investigation_id == investigation.id)))
            manifest = actor.db.get(m.EvidenceManifest, investigation.manifest_id) if investigation.manifest_id else None
            if manifest:
                resources.update(manifest.instance_ids or [])
        # Connection tests and exports can expose private credential/file metadata.
        if row.type not in ('collect_instance', 'collection', 'investigation', 'verification') or not resources or not resources.issubset(ids):
            continue
        if row.type in ('investigation', 'verification') and not resources.issubset(event_ids):
            continue
        progress = {k: v for k, v in (row.progress or {}).items() if k in ('completed', 'total', 'percent', 'row_count') and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
        items.append({**fields(row, ['id', 'created_at', 'started_at', 'finished_at', 'attempt']),
                      'type': safe_text(row.type), 'queue': safe_text(row.queue), 'status': safe_text(row.status),
                      'instance_ids': sorted(resources), 'progress': progress,
                      'error_code': safe_text(row.error_code) if row.error_code else None})
    return items[:100]


def overview(actor, config, **scope):
    ids = visible_ids(actor, **scope)
    permissions = current_permissions(actor)
    objects = list(actor.db.scalars(select(m.Instance).where(m.Instance.id.in_(ids)).order_by(m.Instance.name, m.Instance.id)))
    system_ids = {row.system_id for row in objects}
    systems = [{'id': row.id, 'name': safe_text(row.name), 'environment': safe_text(row.environment), 'model_policy': safe_text(row.model_policy)} for row in actor.db.scalars(select(m.System).where(m.System.id.in_(system_ids)).order_by(m.System.name))]
    policies = []
    for row in actor.db.scalars(select(m.InspectionPolicy).order_by(m.InspectionPolicy.updated_at.desc(), m.InspectionPolicy.id)):
        if not row.instance_ids or not set(row.instance_ids).issubset(ids):
            continue
        from .intelligence import effective_policy_state
        state, _ = effective_policy_state(actor.db, row)
        check = actor.db.get(m.PlatformSetting, 'inspection-status:' + row.id)
        checked_at = (check.value or {}).get('last_checked_at') if check else None
        # The status record is internal; only an ISO timestamp is accepted.
        try:
            checked_at = iso(parse_time(checked_at)) if checked_at else None
        except HTTPException:
            checked_at = None
        policies.append({**fields(row, ['id', 'enabled', 'instance_ids', 'signal_interval_seconds', 'ai_interval_seconds', 'urgent_enabled', 'cooldown_seconds', 'max_daily_runs', 'next_signal_at', 'next_ai_at', 'last_completed_at']), 'mode': safe_text(row.mode), 'status': safe_text(state), 'last_checked_at': checked_at})
    rules = []
    for row in actor.db.scalars(select(m.Rule).order_by(m.Rule.name, m.Rule.id)):
        params = row.parameters or {}
        resources = params.get('instance_ids')
        if not isinstance(resources, list) or not resources or not all(isinstance(i, str) for i in resources) or not set(resources).issubset(ids):
            continue
        thresholds = {k: v for k, v in params.items() if k in ('threshold', 'duration_seconds', 'min_samples', 'window_seconds') and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
        rules.append({'id': row.id, 'name': safe_text(row.name), 'template': safe_text(row.template), 'enabled': row.enabled, 'instance_ids': resources, 'parameters': thresholds})
    model = getattr(config, 'model', None) or {}
    model_view = {k: safe_text(model.get(k)) for k in ('provider', 'model')}
    model_view.update({k: model.get(k) for k in ('max_calls', 'max_tokens') if type(model.get(k)) is int and model[k] >= 0})
    model_view['configured'] = bool(model.get('model'))
    tasks = _tasks(actor, ids)
    now = utcnow()
    queues = {}
    for row in actor.db.scalars(select(m.WorkerHeartbeat)):
        key = row.queue if row.queue in ('collection', 'ai', 'maintenance') else 'other'
        age = (now - row.last_seen_at).total_seconds()
        queues.setdefault(key, set()).add('available' if 0 <= age < 120 else 'stale' if age >= 120 else 'unknown')
    return {'read_only_projection': True, 'sources': source_summaries(actor, ids), 'systems': systems,
            'objects': [{'id': row.id, 'name': safe_text(row.name), 'system_id': row.system_id, 'enabled': row.enabled, 'lifecycle': safe_text(row.lifecycle_state), 'model_policy': safe_text(row.model_policy)} for row in objects],
            'policies': policies, 'rules': rules, 'model': model_view, 'tasks': tasks,
            'platform': {'control_database': 'available', 'workers': [{'queue': queue, 'status': 'available' if 'available' in states else 'stale' if 'stale' in states else 'unknown'} for queue, states in sorted(queues.items())], 'task_window': 'latest_100_authorized_from_recent_501', 'updated_at': iso(now)},
            'access': {'read_only': not bool(permissions - READ_ACTIONS), 'roles': [safe_text(r) for r in current_roles(actor)], 'permissions': sorted(permissions & READ_ACTIONS), 'object_count': len(ids), 'system_count': len(systems), 'write_enabled': False, 'sensitive_fields_omitted': True}}
