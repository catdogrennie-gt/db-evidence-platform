"""Server-only inventory of point projections in one exact model request.

This is delivery provenance, not source qualification or semantic admission.
It adds no model input and never joins earlier requests' inventories.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

VERSION = 'request-delivery-v1'
MAX_OBSERVATIONS = 1024
MAX_AUDIT_BYTES = 2 * 1024 * 1024
READONLY_LOCK_TOOLS = frozenset(('get_lock_graph', 'get_evidence'))


def request_sha256(payload):
    # Same canonical encoding as ModelCall.input_sha256 and existing audits.
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _object(content):
    if not isinstance(content, str):
        return {}
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _point_projection(item):
    """Record supplied fields only; absence never supplies completeness/zero."""
    result = {name: _text(item.get(name)) for name in
        ('instance_id', 'source', 'source_mode', 'observed_at', 'template')}
    data = item.get('data') if isinstance(item.get('data'), dict) else {}
    meta = data.get('item') if isinstance(data.get('item'), dict) else {}
    result['item'] = {name: _text(meta.get(name)) for name in ('source_id', 'hostid', 'itemid')}
    coverage = item.get('coverage') if isinstance(item.get('coverage'), dict) else {}
    result['availability'] = _text(coverage.get('availability'))
    for name in ('complete', 'source_complete', 'truncated'):
        if type(coverage.get(name)) is bool:
            result['coverage_' + name] = coverage[name]
    if type(data.get('row_count')) is int and data['row_count'] >= 0:
        result['row_count'] = data['row_count']
    for name in ('snapshot_complete', 'snapshot_truncated', 'observation_unavailable', 'text_omitted'):
        if type(data.get(name)) is bool:
            result[name] = data[name]
    for name in ('snapshot_scope', 'projection_scope'):
        if _text(data.get(name)) is not None:
            result[name] = data[name]
    return result


def prepare_request_delivery(payload, *, investigation_id, manifest_id, sequence):
    """Inventory the already-packed request; do not annotate/mutate its input.

    Projection hashes bind all retained fields, including rows not copied into
    this small inventory. Identity/config/hash/authorization revalidation against
    the frozen manifest remains a separate required admission step.
    """
    observations = []
    complete = True
    pending = {}
    used = set()
    messages = payload.get('messages', [])
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        if role == 'assistant':
            for tool in message.get('tool_calls') or []:
                if not isinstance(tool, dict):
                    continue
                key = tool.get('id')
                function = tool.get('function') or {}
                if _text(key) is not None:
                    pending[key] = (function.get('name') in READONLY_LOCK_TOOLS
                                    and key not in pending and key not in used)
        if index == 1 and role == 'user':
            items = _object(message.get('content')).get('evidence', [])
        elif role == 'tool' and pending.get(message.get('tool_call_id')) is True:
            key = message['tool_call_id']
            pending[key] = False
            used.add(key)
            items = _object(message.get('content')).get('items', [])
        else:
            continue
        if not isinstance(items, list):
            complete = False
            continue
        for item_index, item in enumerate(items):
            if not isinstance(item, dict) or item.get('source') != 'mysql.lock_waits':
                continue
            if len(observations) >= MAX_OBSERVATIONS:
                complete = False
                continue
            ref = _text(item.get('evidence_id'))
            if ref is None:
                complete = False
                continue
            observations.append({'evidence_id': ref,
                'location': {'message_index': index, 'item_index': item_index},
                'projection_sha256': request_sha256(item),
                'point': _point_projection(item)})
    result = {'version': VERSION, 'status': 'prepared',
        'investigation_id': investigation_id, 'manifest_id': manifest_id, 'sequence': sequence,
        'request_sha256': request_sha256(payload), 'projection_inventory_complete': complete,
        'observations': observations, 'semantic_verification': 'not_performed'}
    result['inventory_sha256'] = request_sha256(result)
    return result


def confirm_request_delivery(receipt, payload, audit_path):
    """A matching saved response proves provision, never model comprehension.

    The digest is consistency checking within server-owned metadata, not a
    signature and not authority to accept a receipt supplied by a client/model.
    """
    result = deepcopy(receipt)
    result['status'] = 'unconfirmed'
    try:
        expected = prepare_request_delivery(payload, investigation_id=receipt['investigation_id'],
            manifest_id=receipt['manifest_id'], sequence=receipt['sequence'])
        if receipt != expected:
            return result
        path = Path(audit_path)
        if path.stat().st_size > MAX_AUDIT_BYTES:
            return result
        raw = path.read_bytes()
        audit = json.loads(raw)
        if (not isinstance(audit, dict) or not isinstance(audit.get('response'), dict)
                or not isinstance(audit.get('request'), dict)
                or request_sha256(audit['request']) != receipt['request_sha256']):
            return result
    except (OSError, ValueError, TypeError, KeyError):
        return result
    result.update(status='response_received', audit_sha256=hashlib.sha256(raw).hexdigest())
    return result
