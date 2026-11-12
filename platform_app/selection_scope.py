"""Server-captured selection fences for new automatic investigations only."""
import hashlib
from sqlalchemy import and_, func, or_, select, true
from . import models as m
from .common import PlatformError

CONTRACT = 'selected-monitoring-evidence-v1'


def _changed():
    return PlatformError(409, 'selection_changed', '巡检数据选择已变化，请依据当前选择重新排队。')


def _item(item):
    return {'hostid': str(item['hostid']), 'itemid': str(item['itemid']),
            'value_kind': item.get('value_kind', 'unsupported'),
            'enhancement': item.get('enhancement'), 'units': item.get('units') or '',
            'value_semantics': item.get('value_semantics') or 'unknown'}


def _instances(scope):
    if scope is None:
        return {}
    if (not isinstance(scope, dict) or scope.get('contract') != CONTRACT
            or not isinstance(scope.get('instances'), dict) or not scope['instances']):
        raise _changed()
    return scope['instances']


def capture_selection_scope(db, instance_ids, *, lock=False):
    """Capture safe identifiers, never connection credentials or new evidence."""
    result = {}
    for identifier in sorted(set(instance_ids)):
        query = select(m.Instance).where(m.Instance.id == identifier).execution_options(populate_existing=True)
        instance = db.scalar(query.with_for_update() if lock else query)
        query = select(m.MonitoringMapping).where(
            m.MonitoringMapping.instance_id == identifier,
            m.MonitoringMapping.layer == 'group').execution_options(populate_existing=True)
        mapping = db.scalar(query.with_for_update() if lock else query)
        if mapping is None:
            continue
        if instance is None or not instance.enabled or not mapping.selected or not mapping.items:
            raise _changed()
        try:
            items = sorted((_item(item) for item in mapping.items), key=lambda item: (item['hostid'], item['itemid']))
        except (KeyError, TypeError, AttributeError):
            raise _changed() from None
        if len({(item['hostid'], item['itemid']) for item in items}) != len(items):
            raise _changed()
        result[identifier] = {'source_id': mapping.source_id, 'mapping_id': mapping.id,
                              'mapping_version': mapping.version, 'config_revision': instance.config_revision,
                              'items': items}
    return {'contract': CONTRACT, 'instances': result} if result else None


def validate_selection_scope(db, scope, instance_ids, lock=False):
    # Re-read with populate_existing: expire_on_commit=False must not pin a stale
    # ORM mapping while a different session saves a new selection.
    actual = capture_selection_scope(db, instance_ids, lock=lock)
    if scope != actual:
        raise _changed()


def evidence_selection_predicate(scope):
    """Filter before counts/quotas; retain compatible history of kept items."""
    scopes = _instances(scope)
    if not scopes:
        return true()
    branches = [~m.Evidence.instance_id.in_(list(scopes))]
    for identifier, selected in scopes.items():
        for item in selected['items']:
            identity = ':'.join((selected['source_id'], item['hostid'], item['itemid']))
            sources = ['zabbix.item.' + item['value_kind'] + ':' + hashlib.sha256(identity.encode()).hexdigest()[:24]]
            if item.get('enhancement'):
                sources.append(item['enhancement'])
            payload = m.Evidence.payload['item']
            coverage = m.Evidence.coverage
            branches.append(and_(
                m.Evidence.instance_id == identifier, m.Evidence.source.in_(sources),
                coverage['source_id'].as_string() == selected['source_id'],
                coverage['hostid'].as_string() == item['hostid'], coverage['itemid'].as_string() == item['itemid'],
                coverage['mapping_version'].as_integer() > 0,
                coverage['mapping_version'].as_integer() <= selected['mapping_version'],
                coverage['config_revision'].as_integer() > 0,
                coverage['config_revision'].as_integer() <= selected['config_revision'],
                payload['hostid'].as_string() == item['hostid'], payload['itemid'].as_string() == item['itemid'],
                payload['value_kind'].as_string() == item['value_kind'],
                func.coalesce(payload['enhancement'].as_string(), '') == (item.get('enhancement') or ''),
                func.coalesce(payload['units'].as_string(), '') == item['units'],
                func.coalesce(payload['value_semantics'].as_string(), 'unknown') == item['value_semantics'],
            ))
    return or_(*branches)


def filter_metric_windows(items, scope):
    """Only matching materialization provenance can enter a scoped group."""
    scopes = _instances(scope)
    if not scopes:
        return list(items)
    result = []
    for window in items:
        if not isinstance(window, dict):
            continue
        selected = scopes.get(window.get('instance_id'))
        if selected is None:
            result.append(window)
            continue
        provenance = window.get('provenance') or {}
        metadata = window.get('item') or {}
        if not isinstance(provenance, dict) or not isinstance(metadata, dict):
            continue
        if (window.get('template') != 'zabbix_item_numeric'
                or window.get('owner_collector') != 'monitoring'
                or window.get('source') != 'zabbix_history'
                or provenance.get('source_id') != selected['source_id']
                or type(provenance.get('mapping_version')) is not int
                or provenance['mapping_version'] != selected['mapping_version']
                or type(provenance.get('config_revision')) is not int
                or provenance['config_revision'] != selected['config_revision']):
            continue
        identity = (provenance.get('hostid'), provenance.get('itemid'))
        match = next((item for item in selected['items']
                      if (item['hostid'], item['itemid']) == identity), None)
        if (match is None or match['value_kind'] != 'numeric'
                or metadata.get('hostid') != identity[0] or metadata.get('itemid') != identity[1]
                or _item(metadata) != match or window.get('unit', '') != match['units']):
            continue
        result.append(window)
    return result
