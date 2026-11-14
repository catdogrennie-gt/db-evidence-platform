"""Directions require the same authorized source row and exact delivered row."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.platform.test_ai_scope import ai_env
from tests.platform.test_report_grounding import prepare_case, ground


def test_real_grounding_adds_direction_without_claiming_model_prose_verified(ai_env):
    e = prepare_case(ai_env)
    before = deepcopy(e['result'].report)
    result = ground(e)
    fact, = result['admitted_facts']
    assert fact['lock_relations'] == [{'blocking_alias': 'A', 'waiting_alias': 'B'}]
    assert result['model_narrative_verification'] == 'unverified'
    assert e['result'].report == before


@pytest.mark.parametrize('change', ['missing', 'reversed', 'omitted', 'boolean', 'other_id'])
def test_actual_packed_row_cannot_supply_a_direction_not_in_source(ai_env, monkeypatch, change):
    from platform_app import model_context
    original = model_context.compact_initial

    def alter(bundle):
        result = original(bundle)
        for item in result['evidence']:
            if item.get('source') != 'mysql.lock_waits':
                continue
            rows = item['data']['rows']
            if change == 'missing': rows[0].pop('blocking_trx_id', None)
            if change == 'reversed': rows[0].update(blocking_trx_id=1, waiting_trx_id=2)
            if change == 'omitted': item['data']['rows'] = []
            if change == 'boolean': rows[0]['blocking_trx_id'] = True
            if change == 'other_id': rows[0]['blocking_trx_id'] = 9
        return result

    monkeypatch.setattr(model_context, 'compact_initial', alter)
    result = ground(prepare_case(ai_env))
    assert result['status'] == 'point_facts_only'
    assert all(not f.get('lock_relations') for f in result['admitted_facts'])


def test_repeat_delivery_with_different_roles_cannot_cherry_pick_one_projection(ai_env, monkeypatch):
    from platform_app import model_context
    original = model_context.compact_initial

    def duplicate(bundle):
        result = original(bundle)
        item = deepcopy(next(v for v in result['evidence'] if v.get('source') == 'mysql.lock_waits'))
        item['data']['rows'][0].update(blocking_trx_id=1, waiting_trx_id=2)
        result['evidence'].append(item)
        return result

    monkeypatch.setattr(model_context, 'compact_initial', duplicate)
    assert all(not f.get('lock_relations') for f in ground(prepare_case(ai_env))['admitted_facts'])


@pytest.mark.parametrize('other_identity', [False, True])
def test_undelivered_other_item_cannot_supply_or_veto_original_relationship(ai_env, monkeypatch, other_identity):
    from platform_app import models as m, model_context
    from platform_app.evidence import digest

    def add_other(row):
        payload = deepcopy(row.payload)
        if other_identity:
            payload['item']['itemid'] = '901'
            payload['diagnostic_origin']['collection']['itemid'] = '901'
        payload['rows'][0].update(blocking_trx_id=1, waiting_trx_id=2) if other_identity else None
        other = m.Evidence(instance_id=row.instance_id, run_id=row.run_id, source=row.source,
            source_mode='live', observed_at=row.observed_at, collector_version='zabbix-generic-1',
            payload=payload, payload_sha256=digest(payload), coverage=deepcopy(row.coverage), truncated=False)
        ai_env['db'].add(other); ai_env['db'].flush(); ai_env['hidden_id'] = other.id

    original = model_context.compact_initial

    def omit(bundle):
        result = original(bundle)
        result['evidence'] = [item for item in result['evidence'] if item['evidence_id'] != ai_env['hidden_id']]
        return result

    monkeypatch.setattr(model_context, 'compact_initial', omit)
    result = ground(prepare_case(ai_env, alter=add_other))
    fact, = result['admitted_facts']
    assert fact['lock_relations'] == [{'blocking_alias': 'A', 'waiting_alias': 'B'}]
    assert 'conflicting_same_time_lock_relations' not in result['reason_codes']


def test_same_time_equal_count_opposite_direction_in_unsent_point_vetoes_graph(ai_env, monkeypatch):
    from platform_app import models as m, model_context
    from platform_app.evidence import digest

    def add_conflict(row):
        payload = deepcopy(row.payload)
        payload['rows'][0].update(blocking_trx_id=1, waiting_trx_id=2)
        other = m.Evidence(instance_id=row.instance_id, run_id=row.run_id, source=row.source,
            source_mode='live', observed_at=row.observed_at, collector_version='zabbix-generic-1',
            payload=payload, payload_sha256=digest(payload), coverage=deepcopy(row.coverage), truncated=False)
        ai_env['db'].add(other)
        ai_env['db'].flush()
        ai_env['hidden_id'] = other.id

    original = model_context.compact_initial

    def omit(bundle):
        result = original(bundle)
        result['evidence'] = [item for item in result['evidence'] if item['evidence_id'] != ai_env['hidden_id']]
        return result

    monkeypatch.setattr(model_context, 'compact_initial', omit)
    result = ground(prepare_case(ai_env, alter=add_conflict))
    assert result['admitted_facts']  # Row-count facts survive; direction does not.
    assert not any(f.get('lock_relations') for f in result['admitted_facts'])
    assert 'conflicting_same_time_lock_relations' in result['reason_codes']


@pytest.mark.parametrize('sensitivity', ['unknown', 'future'])
def test_only_current_redacted_evidence_can_expose_operational_transaction_ids(ai_env, sensitivity):
    e = prepare_case(ai_env, alter=lambda row: setattr(row, 'sensitivity', sensitivity))
    assert all(not f.get('lock_relations') for f in ground(e)['admitted_facts'])


def test_sensitivity_change_during_final_authorization_hides_relationship(ai_env, monkeypatch):
    from platform_app import investigations
    e = prepare_case(ai_env)
    original = investigations.get_investigation
    calls = 0

    def changed(actor, identifier):
        nonlocal calls
        calls += 1
        if calls == 2:
            e['evidence'].sensitivity = 'unknown'
            e['db'].commit()
        return original(actor, identifier)

    monkeypatch.setattr(investigations, 'get_investigation', changed)
    assert all(not f.get('lock_relations') for f in ground(e)['admitted_facts'])


def test_report_changed_during_added_field_policy_read_withdraws_all_facts(ai_env, monkeypatch):
    e = prepare_case(ai_env)
    execute = e['db'].execute
    changed = False

    def read(statement, *args, **kwargs):
        nonlocal changed
        if not changed and str(statement).startswith('SELECT evidence.id, evidence.sensitivity'):
            changed = True
            e['result'].report = {**e['result'].report, 'unknowns': ['Changed during field-policy read']}
            e['db'].commit()
        return execute(statement, *args, **kwargs)

    monkeypatch.setattr(e['db'], 'execute', read)
    result = ground(e)
    assert changed
    assert result['status'] == 'unverified' and not result['admitted_facts']
    assert result['reason_codes'] == ['report_changed_during_validation']


def sample(raw, supplied=None):
    from platform_app.request_delivery import request_sha256
    item = {'evidence_id': 'point-a', 'data': {'row_count': len(raw), 'rows': deepcopy(raw if supplied is None else supplied)}}
    payload = {'messages': [{}, {'content': __import__('json').dumps({'evidence': [item]})}]}
    observation = {'evidence_id': 'point-a', 'location': {'message_index': 1, 'item_index': 0},
        'projection_sha256': request_sha256(item)}
    return SimpleNamespace(payload={'rows': raw}), observation, payload


def relations(args):
    from platform_app import report_grounding
    return report_grounding._delivered_lock_relations(*args)


def test_transaction_identity_is_lossless_and_graph_is_deduplicated():
    row = {'blocking_trx_id': '18446744073709551615', 'waiting_trx_id': 1}
    assert relations(sample([row, row])) == (('18446744073709551615', '1'),)


@pytest.mark.parametrize('bad', [True, None, 0, -1, 'not-an-id', '18446744073709551616', 9007199254740992.0])
def test_unsafe_or_unknown_ids_do_not_form_edges(bad):
    assert relations(sample([{'blocking_trx_id': bad, 'waiting_trx_id': 1}])) == ()


def test_location_hash_and_row_completeness_are_required():
    raw = [{'blocking_trx_id': 2, 'waiting_trx_id': 1}]
    for change in ('hash', 'message', 'item', 'identity', 'count'):
        row, obs, payload = sample(raw)
        if change == 'hash': obs['projection_sha256'] = 'wrong'
        if change == 'message': obs['location']['message_index'] = 9
        if change == 'item': obs['location']['item_index'] = 9
        if change == 'identity': obs['evidence_id'] = 'other'
        if change == 'count':
            import json
            from platform_app.request_delivery import request_sha256
            item = json.loads(payload['messages'][1]['content'])['evidence'][0]
            item['data']['row_count'] = 2
            payload['messages'][1]['content'] = json.dumps({'evidence': [item]})
            obs['projection_sha256'] = request_sha256(item)
        assert relations((row, obs, payload)) == ()


def test_empty_partial_and_missing_roles_do_not_create_graphs():
    assert relations(sample([])) == ()
    assert relations(sample([{'blocking_trx_id': 2}])) == ()
    assert relations(sample([{'blocking_trx_id': 2, 'waiting_trx_id': 1}] * 51)) == ()
    assert relations(sample([{'blocking_trx_id': 2, 'waiting_trx_id': 1}], supplied=[])) == ()


def test_restricted_snapshot_provides_only_point_local_aliases_not_source_ids(ai_env):
    import json
    def restricted(row):
        row.sensitivity = 'restricted'
        row.payload['rows'][0].update(blocking_trx_id=883552, waiting_trx_id=772441)
    fact, = ground(prepare_case(ai_env, alter=restricted))['admitted_facts']
    assert fact['lock_relation_identity'] == 'point_local_aliases'
    assert fact['lock_relations'] == [{'blocking_alias': 'A', 'waiting_alias': 'B'}]
    extra = json.dumps({'identity': fact['lock_relation_identity'], 'relations': fact['lock_relations']})
    assert '883552' not in extra and '772441' not in extra
    assert 'thread' not in extra and 'transaction' not in extra


def test_aliases_have_no_identity_across_separate_points():
    from platform_app.report_grounding import _anonymous_lock_relations
    assert _anonymous_lock_relations((('883552', '772441'),)) == _anonymous_lock_relations((('10', '20'),))
