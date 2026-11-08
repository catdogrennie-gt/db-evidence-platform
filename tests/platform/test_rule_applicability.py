from datetime import timedelta
import pytest
from sqlalchemy import select
from tests.platform.test_events import db,graph,observe
from platform_app import models as m
from platform_app.db import utcnow
from platform_app.events import clock_tick


@pytest.mark.parametrize('engine',['host','network'])
def test_non_database_object_with_real_host_samples_never_fires_mysql_core_stale(db,engine):
    _,a,_,_=graph(db)
    a.connection_mode='zabbix';a.engine=engine;a.expected_server_uuid=None;a.created_at=utcnow()-timedelta(hours=1)
    db.add(m.CollectionSchedule(instance_id=a.id,collector='host',interval_seconds=60))
    observe(db,a,utcnow()-timedelta(seconds=1),source='linux.host',collector='host',rows=[])
    clock_tick(db)
    assert db.scalar(select(m.Event).where(m.Event.instance_id==a.id,m.Event.title=='核心采集过期或失联')) is None


def test_external_source_before_first_scheduled_sample_is_unknown_not_database_failure(db):
    _,a,_,_=graph(db)
    a.connection_mode='zabbix';a.created_at=utcnow()-timedelta(hours=1)
    db.add(m.CollectionSchedule(instance_id=a.id,collector='identity_status',interval_seconds=60))
    clock_tick(db)
    assert db.scalar(select(m.Event).where(m.Event.instance_id==a.id)) is None


def test_inapplicable_existing_alert_is_preserved_without_claiming_recovery(db):
    _,a,_,_=graph(db)
    now=utcnow()
    observe(db,a,now-timedelta(minutes=5),source='mysql.global_status',collector='identity_status',rows=[])
    clock_tick(db,now=now)
    event=db.scalar(select(m.Event).where(m.Event.instance_id==a.id,m.Event.title=='核心采集过期或失联'))
    assert event is not None
    original=event.id
    a.engine='host';a.connection_mode='zabbix'
    db.add(m.CollectionSchedule(instance_id=a.id,collector='host',interval_seconds=60))
    clock_tick(db,now=now+timedelta(seconds=16))
    db.flush()
    assert db.get(m.Event,original).status=='closed'
    assert db.get(m.Event,original).closure_type=='not_applicable'
    occurrence=db.scalar(select(m.AlertOccurrence).join(m.EventAlert,m.EventAlert.alert_occurrence_id==m.AlertOccurrence.id).where(m.EventAlert.event_id==original))
    assert occurrence.status=='not_applicable' and occurrence.recovered_at is None
