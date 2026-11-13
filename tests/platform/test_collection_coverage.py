"""Coverage notices describe selected, authorized facts, not absent value types."""
from datetime import timedelta, timezone
from types import SimpleNamespace
import pytest
from platform_app.db import utcnow
from tests.platform.test_analysis_groups import group_env, monitoring_env, ai_env


def item(**changes):
    return dict(itemid='1',host_name='Linux',name='CPU',value_kind='numeric',
        available=True,status='0',state='0',lastclock=str(int(utcnow().replace(tzinfo=timezone.utc).timestamp())),
        interval=60,interval_verified=True,**changes)


def summary(items,**changes):
    from platform_app.collection_coverage import selected_coverage
    instance=SimpleNamespace(id='a',name='Orders')
    mapping=SimpleNamespace(id='group-a',source_id='source',items=items,
        capabilities=[{'name':'log','status':'missing'}])
    return selected_coverage([instance],{'a':mapping},now=utcnow(),**changes)


def test_absent_unselected_log_type_does_not_become_a_gap():
    result=summary([item()])
    assert result['missing_count']==0 and result['items']==[]
    assert result['status']!='complete'  # fresh metadata is not database health


@pytest.mark.parametrize('change,reason',[
    ({'available':False,'state':'1','error':'secret raw SQL'},'item_unsupported'),
    ({'available':False,'status':'1'},'item_disabled'),
    ({'lastclock':'0'},'no_samples'),
    ({'lastclock':'1'},'sample_stale'),
    ({'interval_verified':False,'history_semantics':'sparse_on_change'},'sampling_interval_unverified'),
])
def test_gaps_have_object_item_reason_and_no_upstream_error_body(change,reason):
    selected=item();selected.update(change)
    result=summary([selected]);[gap]=result['items']
    assert gap['instance_id']=='a' and gap['object_name']=='Orders'
    assert gap['itemid']=='1' and gap['item_name']=='CPU' and gap['reason']==reason
    assert 'secret raw SQL'not in str(result)


def test_sparse_heartbeat_uses_retained_interval_not_poll_interval():
    selected=item();selected.update(history_semantics='sparse_on_change',interval=3600,
        heartbeat_seconds=3600,poll_interval=60,
        lastclock=str(int((utcnow()-timedelta(minutes=20)).replace(tzinfo=timezone.utc).timestamp())))
    assert summary([selected])['items']==[]


def test_details_are_bounded_but_authorized_count_is_exact():
    selected=item();selected.update(lastclock='0')
    result=summary([dict(selected,itemid=str(i))for i in range(205)])
    assert result['missing_count']==205 and len(result['items'])==200
    assert result['truncated']is True


def test_overview_returns_only_visible_object_gaps_without_upstream_calls(group_env):
    from platform_app.analysis_groups import save_group
    from platform_app.intelligence import overview
    e=group_env
    group=save_group(e['actor'],e['config'],e['body']);e['db'].commit();e['fake'].calls=[]
    result=overview(e['actor'],instance_id=group['instance_id'])
    assert result['coverage']['items']
    assert all(x['instance_id']==group['instance_id']for x in result['coverage']['items'])
    assert e['fake'].calls==[]
    result=overview(e['actor'],instance_id='unauthorized-instance')
    assert result['coverage']['missing_count']==0 and result['coverage']['items']==[]
