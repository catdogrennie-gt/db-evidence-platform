"""A later unrelated periodic report must not hide a bound episode diagnosis."""
from datetime import timedelta

import pytest

from platform_app import models as m
from tests.platform.test_ai_scope import ai_env
from tests.platform.test_showcase import showcase_env
from tests.platform.test_health_observation_guards import frozen_manifest
from tests.platform.test_health_signals import numeric_evidence,series


@pytest.fixture
def episode_report(showcase_env,monkeypatch):
    from platform_app import showcase
    from platform_app.health_signals import scan_group_changes
    e=showcase_env
    e['config'].showcase['projection_mode']='numeric'
    now,points=series()
    numeric_evidence(e,points)
    [finding]=scan_group_changes(e['db'],e['policy'],now)
    frozen_manifest(e)
    bound=e['investigation'];bound.status='completed';bound.event_id=finding.event_id
    bound.created_at=now-timedelta(minutes=1)
    bound.report={'executive_summary':{'headline':'Diagnosis bound to this episode'}}
    finding.latest_investigation_id=bound.id
    e['db'].commit()
    monkeypatch.setattr(showcase,'db_now',lambda db:now)
    e.update(now=now,finding=finding,bound=bound)
    return e


def newer_periodic(e,offset=0):
    inv=m.Investigation(created_by=e['actor'].user_id,question='Periodic inspection',
        status='completed',permission_version=1,created_at=e['now']+timedelta(seconds=offset),
        window_start=e['now']-timedelta(minutes=20),window_end=e['now'],
        report={'executive_summary':{'headline':'Other periodic observations'}})
    e['db'].add(inv);e['db'].flush()
    e['db'].add(m.InvestigationResource(investigation_id=inv.id,instance_id=e['a'].id))
    e['db'].commit()
    return inv


@pytest.mark.parametrize('periodic_count',[1,21])
def test_bound_current_episode_report_wins_over_newer_unrelated_periodic(episode_report,periodic_count):
    from platform_app.showcase import snapshot
    e=episode_report
    for index in range(periodic_count):newer_periodic(e,index)
    value=snapshot(e['db'],e['config'])
    assert value['report']['id']==e['bound'].id
    assert value['report_matches_current_observation'] is True
    assert value['report']['current_for_episode'] is True
    [signal]=value['report']['matched_signals']
    assert signal['id']==e['finding'].id
    assert signal['event_id']==e['finding'].event_id
    assert (signal['hostid'],signal['itemid'])==('101','1234')
    assert signal['episode_started_at']==e['finding'].calculation['episode_started_at']


def test_preferred_report_with_hidden_resource_cannot_cross_exact_target_scope(episode_report):
    from platform_app.showcase import snapshot
    e=episode_report
    fallback=newer_periodic(e)
    e['db'].add(m.InvestigationResource(investigation_id=e['bound'].id,instance_id=e['b'].id))
    e['db'].commit()
    value=snapshot(e['db'],e['config'])
    assert value['report']['id']==fallback.id
    assert value['report']['matched_signals']==[]
    assert value['report_matches_current_observation'] is False


def test_previous_episode_report_cannot_be_promoted_by_same_finding_pointer(episode_report):
    from platform_app.showcase import snapshot
    e=episode_report
    fallback=newer_periodic(e)
    e['bound'].window_end=e['now']-timedelta(seconds=30)
    e['db'].commit()
    value=snapshot(e['db'],e['config'])
    assert value['report']['id']==fallback.id
    assert value['report']['matched_signals']==[]
    assert value['report_matches_current_observation'] is False


def test_returned_observation_keeps_latest_history_without_current_match(episode_report):
    from platform_app.showcase import snapshot
    e=episode_report
    fallback=newer_periodic(e)
    e['finding'].calculation={**e['finding'].calculation,'observation_state':'returned_to_baseline'}
    e['db'].commit()
    value=snapshot(e['db'],e['config'])
    assert value['report']['id']==fallback.id
    assert value['report']['matched_signals']==[]
    assert value['report_matches_current_observation'] is False
