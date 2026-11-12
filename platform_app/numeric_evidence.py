"""Bounded reads of actual current numeric collection identities, never a cache."""
import hashlib

from sqlalchemy import select

from . import models as m


def latest_numeric_evidence(db,instance_id,source_id,selected,*,observed_since=None):
    """Load at most one actual payload per selected source/host/item.

    Select the source-latest row before validating its payload/provenance. An
    unavailable or invalid newest collection must never reveal an older good
    value as current. Callers still validate mapping/config revisions, freshness
    and availability according to their own projection/analysis contract.
    """
    selected={(str(hostid),str(itemid)) for hostid,itemid in selected}
    if not selected:return []
    sources={
        'zabbix.item.numeric:'+hashlib.sha256(':'.join((source_id,hostid,itemid)).encode()).hexdigest()[:24]:
            (hostid,itemid)
        for hostid,itemid in selected}
    # Each scalar lookup uses ix_evidence_instance_source_time and stops at the
    # latest matching live row. Avoid JSON extraction/window sorting across the
    # complete retained history, and deserialize only the selected payloads.
    latest_ids=[select(m.Evidence.id).where(m.Evidence.instance_id==instance_id,
        m.Evidence.source==source,m.Evidence.source_mode=='live'
    ).order_by(m.Evidence.observed_at.desc(),m.Evidence.id.desc()).limit(1).scalar_subquery()
        for source in sorted(sources)]
    statement=select(m.Evidence).where(m.Evidence.instance_id==instance_id,m.Evidence.id.in_(latest_ids))
    if observed_since is not None:statement=statement.where(m.Evidence.observed_at>=observed_since)
    rows=db.scalars(statement.order_by(m.Evidence.observed_at.desc(),m.Evidence.id.desc()))
    result=[]
    for row in rows:
        if not isinstance(row.payload,dict) or not isinstance(row.coverage,dict):continue
        meta=row.payload.get('item');coverage=row.coverage
        if not isinstance(meta,dict):continue
        identity=(str(meta.get('hostid')),str(meta.get('itemid')))
        if identity!=sources.get(row.source) or coverage.get('source_id')!=source_id:continue
        result.append(row)
    return result
