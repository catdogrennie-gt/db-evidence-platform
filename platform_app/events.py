"""Deterministic observations, preserved alert episodes and human workflow.

All writes participate in the caller's transaction. This module never commits,
executes target operations, sends notifications or treats unknown as recovery.
"""

from datetime import timedelta
import hashlib
from uuid import uuid4
from sqlalchemy import select, exists, or_
from . import models as m
from .access import authorize, actor_for_user
from .audit import append_audit
from .common import PlatformError, fields, iso, json_safe, page_query, time_window
from .db import utcnow
from .evidence import create_manifest, digest

EVENT_FIELDS = [
    "id",
    "instance_id",
    "title",
    "status",
    "severity",
    "source",
    "assigned_to",
    "created_by",
    "version",
    "coverage_revision",
    "closure_type",
    "closure_reason",
    "last_seen_at",
    "created_at",
    "updated_at",
    "manifest_id",
    "merged_into_event_id",
]
DEFAULTS = (
    (
        "locks_present:v1",
        "持续锁等待",
        "locks_present",
        {
            "collector": "runtime",
            "threshold": 0,
            "min_duration_seconds": 60,
            "freshness_multiplier": 1.5,
        },
    ),
    (
        "sql_latency:v1",
        "SQL摘要窗口延迟",
        "sql_latency",
        {
            "collector": "statement_digest",
            "threshold": 0.5,
            "min_executions": 10,
            "min_duration_seconds": 60,
            "freshness_multiplier": 1.5,
        },
    ),
    (
        "collector_stale:v1",
        "核心采集过期或失联",
        "collector_stale",
        {
            "collector": "identity_status",
            "stale_intervals": 3,
            "min_duration_seconds": 0,
            "freshness_multiplier": 1.5,
        },
    ),
)


def _safe_severity(value):
    return (
        value
        if isinstance(value, str) and value in ("low", "medium", "high", "critical", "warning")
        else "warning"
    )


def _highest_severity(values):
    rank = {"low": 1, "warning": 2, "medium": 2, "high": 3, "critical": 4}
    return max((_safe_severity(value) for value in values), key=rank.__getitem__, default="warning")


def ensure_default_rules(db):
    for version, name, template, parameters in DEFAULTS:
        if not db.scalar(select(m.Rule.id).where(m.Rule.rule_version == version)):
            db.add(
                m.Rule(
                    name=name,
                    rule_version=version,
                    template=template,
                    parameters=parameters,
                    enabled=True,
                )
            )
    db.flush()


def event_instances(db, event):
    ids = {event.instance_id}
    ids.update(
        db.scalars(
            select(m.AlertOccurrence.instance_id)
            .join(m.EventAlert, m.EventAlert.alert_occurrence_id == m.AlertOccurrence.id)
            .where(m.EventAlert.event_id == event.id)
        )
    )
    ids.update(
        db.scalars(select(m.ManualChange.instance_id).where(m.ManualChange.event_id == event.id))
    )
    if event.manifest_id:
        manifest = db.get(m.EvidenceManifest, event.manifest_id)
        if manifest:
            ids.update(manifest.instance_ids)
    return ids


def get_event(actor, event_id, action="event.read", *, lock=False):
    query = select(m.Event).where(m.Event.id == event_id)
    if lock:
        query = query.with_for_update()
    event = actor.db.scalar(query.execution_options(populate_existing=True))
    if not event:
        raise PlatformError(404, "not_found", "事件不存在。")
    ids = event_instances(actor.db, event)
    authorize(actor, "data.read", ids)
    authorize(actor, "event.read", ids)
    if action != "event.read":
        authorize(actor, action, ids)
    return event


def check_version(event, body):
    expected = body.get("expected_version", body.get("version"))
    if expected != event.version:
        raise PlatformError(
            409, "version_conflict", "事件已更新，请刷新。", {"version": event.version}
        )


def _transition(db, event, action, actor_id=None, details=None, evaluation_id=None, to_state=None):
    old = event.status
    if to_state:
        event.status = to_state
    event.version += 1
    db.add(
        m.EventTransition(
            event_id=event.id,
            actor_id=actor_id,
            evaluation_id=evaluation_id,
            action=action,
            from_state=old,
            to_state=event.status,
            details=json_safe(details or {}),
        )
    )


def source_manifest_ids(db, event):
    event_ids = {event.id}
    pending = [event.id]
    while pending:
        identifier = pending.pop()
        for row in db.scalars(
            select(m.EventTransition).where(
                m.EventTransition.event_id == identifier, m.EventTransition.action == "merge"
            )
        ):
            for source_id in row.details.get("source_event_ids", []):
                if source_id not in event_ids:
                    event_ids.add(source_id)
                    pending.append(source_id)
                    if len(event_ids) > 100:
                        raise PlatformError(
                            409, "merge_history_limit", "归并历史超过受限遍历预算。"
                        )
    values = list(
        db.scalars(
            select(m.EvidenceManifest.id)
            .where(
                m.EvidenceManifest.owner_type == "event", m.EvidenceManifest.owner_id.in_(event_ids)
            )
            .limit(1001)
        )
    )
    if len(values) > 1000:
        raise PlatformError(409, "manifest_history_limit", "事件证据版本超过受限保全预算。")
    return set(values)


def _snapshot(db, event, now=None):
    stamp = now or utcnow()
    earliest = event.created_at or stamp
    start = min(earliest, stamp) - timedelta(minutes=5)
    # An explicit link preserves the original source window even when it lies
    # outside the target event's current snapshot window.
    prior_ids = source_manifest_ids(db, event)
    manifest = create_manifest(
        db, "event", event.id, event_instances(db, event), start, stamp + timedelta(microseconds=1)
    )
    event.manifest_id = manifest.id
    for identifier in prior_ids:
        prior = db.get(m.EvidenceManifest, identifier)
        prior.retain_until = max(prior.retain_until, manifest.retain_until)
        for pin in db.scalars(select(m.EvidencePin).where(m.EvidencePin.manifest_id == identifier)):
            pin.retain_until = prior.retain_until
        payload = {"manifest_id": identifier, "relation": "preserved_event_source"}
        db.add(
            m.ManifestItem(
                manifest_id=manifest.id,
                instance_id=event.instance_id,
                item_type="manifest_reference",
                payload=payload,
                payload_sha256=digest(payload),
            )
        )
    db.flush()


def event_episodes(db, event):
    return list(
        db.scalars(
            select(m.AlertOccurrence)
            .join(m.EventAlert, m.EventAlert.alert_occurrence_id == m.AlertOccurrence.id)
            .where(m.EventAlert.event_id == event.id)
            .order_by(m.AlertOccurrence.first_seen_at)
        )
    )


def episodes_recovered(db, event, now=None):
    now = now or utcnow()
    episodes = event_episodes(db, event)
    if not episodes:
        return False
    for episode in episodes:
        evidence = episode.evidence or {}
        last = evidence.get("latest") or {}
        if (
            episode.status != "recovered"
            or not episode.recovered_at
            or not last.get("recovery_observation")
        ):
            return False
        from .common import parse_time

        stamp = last.get("recovery_observed_at") or last.get("source_observed_at")
        if not stamp:
            return False
        age = (now - parse_time(stamp)).total_seconds()
        if age < 0 or age > float(last.get("freshness_seconds", 45)):
            return False
    return True


def _valid_verification_ids(db, event):
    required = {row.id for row in event_episodes(db, event)}
    covered = set()
    ids = []
    for verification in db.scalars(
        select(m.Verification).where(
            m.Verification.event_id == event.id,
            m.Verification.outcome == "improved",
            m.Verification.status == "completed",
        )
    ):
        criteria = verification.criteria or {}
        result = verification.result or {}
        if criteria.get("coverage_revision") != event.coverage_revision or not result.get(
            "frozen_input_valid"
        ):
            continue
        if criteria.get("episode_rule_versions") != {
            row.id: db.get(m.ActiveAlert, row.active_alert_id).rule_version
            for row in event_episodes(db, event)
            if row.id in criteria.get("episode_ids", [])
        }:
            continue
        covered.update(criteria.get("episode_ids", []))
        ids.append(verification.id)
    return ids if required and required.issubset(covered) else []


def _maintenance(db, instance, stamp):
    rows = list(
        db.scalars(
            select(m.MaintenanceWindow).where(
                m.MaintenanceWindow.system_id == instance.system_id,
                or_(
                    m.MaintenanceWindow.instance_id == None,
                    m.MaintenanceWindow.instance_id == instance.id,
                ),
                m.MaintenanceWindow.starts_at <= stamp,
                m.MaintenanceWindow.ends_at > stamp,
            )
        )
    )
    return [row.id for row in rows if (row.policy or {}).get("suppress_notifications", True)]


def _rule_input(db, instance, rule, run_id, now, kind):
    p = rule.parameters or {}
    collector = p.get("collector", "identity_status")
    schedule = db.scalar(
        select(m.CollectionSchedule).where(
            m.CollectionSchedule.instance_id == instance.id,
            m.CollectionSchedule.collector == collector,
        )
    )
    interval = (
        schedule.interval_seconds if schedule else (60 if collector == "statement_digest" else 30)
    )
    fresh = interval * float(p.get("freshness_multiplier", 1.5))
    result = {
        "collector": collector,
        "interval_seconds": interval,
        "freshness_seconds": fresh,
        "source": "target_observation",
        "evidence_ids": [],
    }
    # External objects have explicitly selected logical collectors. A host/probe
    # cannot fail a MySQL core collector it never had or was not meant to run.
    mysql_rule = rule.template in ('locks_present','sql_latency') or collector == 'identity_status'
    if (mysql_rule and instance.engine != 'mysql') or (
        instance.connection_mode == 'zabbix' and (schedule is None or not schedule.enabled)
    ):
        result.update(source='configured_capability',reason='rule_not_applicable',engine=instance.engine)
        return 'not_applicable',result,now
    if rule.template in ("collector_stale", "collector_unreachable"):
        if kind == "observation":
            run = db.get(m.CollectionRun, run_id)
            if not run or run.collector != collector:
                return None
        source_query = select(m.Evidence).where(
            m.Evidence.instance_id == instance.id,
            m.Evidence.source == ("mysql.global_status" if collector == "identity_status" else "linux.host"),
            m.Evidence.source_mode == "live",
            m.Evidence.coverage["collector"].as_string() == collector,
        )
        last = db.scalar(source_query.order_by(m.Evidence.observed_at.desc()).limit(1))
        success = db.scalar(
            source_query.where(m.Evidence.coverage["availability"].as_string() == "available")
            .order_by(m.Evidence.observed_at.desc())
            .limit(1)
        )
        last_at = success.observed_at if success else None
        latest_run = db.scalar(
            select(m.CollectionRun)
            .where(
                m.CollectionRun.instance_id == instance.id, m.CollectionRun.collector == collector
            )
            .order_by(m.CollectionRun.started_at.desc(), m.CollectionRun.id.desc())
            .limit(1)
        )
        latest_failed = (
            latest_run is not None
            and latest_run.status in ("failed", "partial")
            and (last_at is None or latest_run.started_at >= last_at)
        )
        base = last_at or (schedule.created_at if instance.connection_mode == 'zabbix' and schedule else instance.created_at)
        age = max(0, (now - base).total_seconds()) if base else None
        result.update(
            source="platform_clock" if kind == "clock_tick" else "collection_run",
            last_success_at=iso(last_at),
            age_seconds=age,
            recovery_observed_at=iso(last_at),
            source_observed_at=iso(now),
            recovery_observation=last_at is not None
            and age <= fresh
            and last is not None
            and last.coverage.get("availability") == "available"
            and not latest_failed,
            evidence_ids=[success.id] if success else [],
        )
        if age is None:
            return "unknown", result, now
        if instance.connection_mode == 'zabbix' and latest_run is None and age < max(300, interval * 3):
            result['reason']='awaiting_first_scheduled_observation'
            return 'unknown',result,now
        if age >= interval * float(p.get("stale_intervals", 3)):
            return "firing", result, now
        return ("normal" if result["recovery_observation"] else "unknown"), result, now
    source = "mysql.lock_waits" if rule.template == "locks_present" else "mysql.statement_digest"
    query = select(m.Evidence).where(
        m.Evidence.instance_id == instance.id,
        m.Evidence.source == source,
        m.Evidence.source_mode == "live",
    )
    if kind == "observation":
        query = query.where(m.Evidence.run_id == run_id)
    item = db.scalar(query.order_by(m.Evidence.observed_at.desc()).limit(1))
    if item is None:
        return ("unknown", result, now) if kind == "clock_tick" else None
    result.update(evidence_ids=[item.id], source_observed_at=iso(item.observed_at), source=source)
    age = (now - item.observed_at).total_seconds()
    if age < 0 or age > fresh or item.coverage.get("availability") != "available" or item.truncated:
        return "unknown", result, item.observed_at
    if kind == "clock_tick":
        return None  # clock ticks cannot advance a target condition's duration
    if rule.template == "locks_present":
        rows = item.payload.get("rows")
        if not isinstance(rows, list):
            return "unknown", result, item.observed_at
        firing = len(rows) > float(p.get("threshold", 0))
        result.update(
            observed_count=len(rows),
            chains=[
                {
                    k: r.get(k)
                    for k in (
                        "waiting_trx_id",
                        "blocking_trx_id",
                        "waiting_thread_id",
                        "blocking_thread_id",
                    )
                }
                for r in rows[:200]
            ],
            recovery_observation=not firing,
        )
        return ("firing" if firing else "normal"), result, item.observed_at
    rows = (item.payload.get("window") or {}).get("rows", [])
    candidates = []
    for row in rows:
        if p.get("digest") and row.get("DIGEST") != p["digest"]:
            continue
        if p.get("schema") and row.get("SCHEMA_NAME") != p["schema"]:
            continue
        count = row.get("count_delta")
        total = row.get("total_time_seconds_delta")
        if (
            row.get("window_validity") == "valid"
            and count is not None
            and count >= int(p.get("min_executions", 10))
            and total is not None
        ):
            candidates.append((total / count, row))
    if not candidates:
        return "unknown", result, item.observed_at
    latency, row = max(candidates, key=lambda value: value[0])
    firing = latency > float(p.get("threshold", 0.5))
    result.update(
        avg_seconds=latency,
        digest=row.get("DIGEST"),
        schema=row.get("SCHEMA_NAME"),
        count=row.get("count_delta"),
        recovery_observation=not firing,
    )
    return ("firing" if firing else "normal"), result, item.observed_at


def _apply_condition(db, instance, rule, evaluation, condition, result, observed_at, now):
    fingerprint = hashlib.sha256(
        f"{rule.template}:{(rule.parameters or {}).get('digest', 'instance')}:{(rule.parameters or {}).get('collector', '')}".encode()
    ).hexdigest()
    active = db.scalar(
        select(m.ActiveAlert)
        .where(
            m.ActiveAlert.instance_id == instance.id,
            m.ActiveAlert.rule_version == rule.rule_version,
            m.ActiveAlert.dimension_fingerprint == fingerprint,
        )
        .with_for_update()
    )
    if condition == 'not_applicable':
        # Keep the erroneous historical episode and its evidence. This is a
        # scope correction, not a fabricated successful recovery observation.
        if active:
            previous = active.state
            active.state,active.pending_since,active.last_evaluated_at='not_applicable',None,now
            episode=db.get(m.AlertOccurrence,active.current_occurrence_id) if active.current_occurrence_id else None
            if episode and episode.status != 'not_applicable':
                episode.status='not_applicable'
                episode.evidence=dict(episode.evidence,latest=result)
                db.add(m.AlertTransition(occurrence_id=episode.id,evaluation_id=evaluation.id,from_state=previous,to_state='not_applicable',occurred_at=now,details=result))
                for event in db.scalars(select(m.Event).join(m.EventAlert,m.EventAlert.event_id==m.Event.id).where(m.EventAlert.alert_occurrence_id==episode.id,m.Event.status.notin_(['closed','merged']))):
                    event.closure_type='not_applicable'
                    event.closure_reason='规则不适用于该对象的引擎或已配置采集能力；保留历史记录，不代表观测到健康恢复。'
                    _transition(db,event,'rule_not_applicable',evaluation_id=evaluation.id,to_state='closed',details=result)
        return
    if not active:
        active = m.ActiveAlert(
            instance_id=instance.id,
            rule_version=rule.rule_version,
            dimension_fingerprint=fingerprint,
            state="unknown",
        )
        db.add(active)
        db.flush()
    previous = active.state
    episode = (
        db.get(m.AlertOccurrence, active.current_occurrence_id)
        if active.current_occurrence_id
        else None
    )
    if condition == "firing":
        if episode and episode.recovered_at is not None:
            episode = None
        if active.pending_since is None or previous == "unknown" and not episode:
            active.pending_since = observed_at
        duration = (observed_at - active.pending_since).total_seconds()
        if not episode and duration < float((rule.parameters or {}).get("min_duration_seconds", 0)):
            active.state = "pending"
            evaluation.state = "pending"
            active.last_evaluated_at = now
            return
        if not episode:
            episode = m.AlertOccurrence(
                instance_id=instance.id,
                active_alert_id=active.id,
                evaluation_id=evaluation.id,
                status="firing",
                severity=_safe_severity((rule.parameters or {}).get("severity")),
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                evidence={"trigger": result, "latest": result, "rule_version": rule.rule_version},
            )
            db.add(episode)
            db.flush()
            active.current_occurrence_id = episode.id
            event = m.Event(
                instance_id=instance.id,
                title=rule.name,
                source="rule",
                status="open",
                severity=episode.severity,
                last_seen_at=observed_at,
            )
            db.add(event)
            db.flush()
            db.add(m.EventAlert(event_id=event.id, alert_occurrence_id=episode.id))
            db.flush()
            db.add(
                m.EventTransition(
                    event_id=event.id,
                    evaluation_id=evaluation.id,
                    action="rule_trigger",
                    to_state="open",
                    details={"episode_id": episode.id, "rule_version": rule.rule_version},
                )
            )
            _snapshot(db, event, now)
            create_manifest(
                db,
                "alert",
                episode.id,
                [instance.id],
                observed_at - timedelta(minutes=5),
                now + timedelta(microseconds=1),
            )
        else:
            episode.status = "firing"
            episode.last_seen_at = observed_at
            episode.evidence = dict(episode.evidence, latest=result)
        active.state = "firing"
    elif condition == "normal":
        active.pending_since = None
        active.state = "recovered"
        if episode:
            episode.status = "recovered"
            episode.recovered_at = episode.recovered_at or observed_at
            episode.last_seen_at = observed_at
            episode.evidence = dict(
                episode.evidence,
                latest=result,
                recovery_evidence_ids=result.get("evidence_ids", []),
            )
    else:
        active.state = "unknown"
        active.pending_since = None
        if episode:
            if episode.recovered_at is None:
                episode.status = "unknown"
            episode.evidence = dict(episode.evidence, latest=result)
    active.last_evaluated_at = now
    if episode:
        db.add(
            m.AlertTransition(
                occurrence_id=episode.id,
                evaluation_id=evaluation.id,
                from_state=previous,
                to_state=episode.status,
                occurred_at=now,
                details=result,
            )
        )
        db.flush()
        for event in db.scalars(
            select(m.Event)
            .join(m.EventAlert, m.EventAlert.event_id == m.Event.id)
            .where(
                m.EventAlert.alert_occurrence_id == episode.id,
                m.Event.status.notin_(["closed", "merged"]),
            )
        ):
            event.last_seen_at = observed_at
            if episodes_recovered(db, event, now):
                if event.status not in ("pending_verification", "resolved"):
                    _transition(
                        db,
                        event,
                        "rule_recovery",
                        evaluation_id=evaluation.id,
                        to_state="pending_verification",
                    )
                    _snapshot(db, event, now)
            elif event.status in ("pending_verification", "resolved"):
                _transition(
                    db,
                    event,
                    "rule_not_recovered",
                    evaluation_id=evaluation.id,
                    to_state="investigating",
                )


def evaluate(db, instance_id, run_id=None, now=None, evaluation_kind="observation"):
    now = now or utcnow()
    instance = db.scalar(select(m.Instance).where(m.Instance.id == instance_id).with_for_update())
    if not instance:
        return {"evaluations": []}
    if evaluation_kind not in ("observation", "clock_tick"):
        raise ValueError("unsupported_evaluation_kind")
    if evaluation_kind == "observation" and not run_id:
        raise ValueError("run_id_required")
    ensure_default_rules(db)
    output = []
    for rule in db.scalars(select(m.Rule).where(m.Rule.enabled == True)):
        if rule.template not in (
            "locks_present",
            "sql_latency",
            "collector_stale",
            "collector_unreachable",
        ):
            continue
        if (rule.parameters or {}).get("instance_ids") and instance_id not in rule.parameters[
            "instance_ids"
        ]:
            continue
        key = f"{rule.rule_version}:{instance_id}:{run_id if evaluation_kind == 'observation' else int(now.timestamp()) // 15}"
        if db.scalar(select(m.RuleEvaluation.id).where(m.RuleEvaluation.evaluation_key == key)):
            continue
        value = _rule_input(db, instance, rule, run_id, now, evaluation_kind)
        if value is None:
            continue
        condition, result, observed = value
        result["suppressed_by_maintenance"] = _maintenance(db, instance, now)
        evaluation = m.RuleEvaluation(
            instance_id=instance_id,
            rule_id=rule.id,
            rule_version=rule.rule_version,
            run_id=run_id,
            evaluation_kind=evaluation_kind,
            evaluation_key=key,
            state=condition,
            evaluated_at=now,
            result=result,
        )
        db.add(evaluation)
        db.flush()
        _apply_condition(db, instance, rule, evaluation, condition, result, observed, now)
        output.append(
            {"id": evaluation.id, "rule_version": rule.rule_version, "state": evaluation.state}
        )
    db.flush()
    return {"instance_id": instance_id, "evaluations": output}


def clock_tick(db, now=None):
    now = now or utcnow()
    rows = list(
        db.scalars(
            select(m.Instance.id).where(
                m.Instance.enabled == True, m.Instance.lifecycle_state == "Active"
            )
        )
    )
    return {
        "evaluated_at": iso(now),
        "instances": [
            evaluate(db, identifier, now=now, evaluation_kind="clock_tick") for identifier in rows
        ],
    }


def event_command(actor, event_id, command, body):
    actions = {
        "acknowledge": "event.acknowledge",
        "assign": "event.assign",
        "comment": "event.comment",
        "close": "event.close",
        "reopen": "event.close",
    }
    if command not in actions:
        raise PlatformError(422, "invalid_action", "不支持的事件动作。")
    event = get_event(actor, event_id, actions[command], lock=True)
    check_version(event, body)
    if event.status == "merged":
        raise PlatformError(409, "event_merged", "请打开归并后的事件。")
    reason = str(body.get("reason", body.get("body", body.get("text", "")))).strip()[:4000]
    if not reason and command != "assign":
        raise PlatformError(422, "reason_required", "需要说明。")
    state = None
    details = {"reason": reason, "source": "human_assertion"}
    if command == "acknowledge":
        if event.status == "closed":
            raise PlatformError(409, "invalid_state", "已关闭事件不能确认。")
        state = "acknowledged" if event.status == "open" else event.status
    elif command == "assign":
        user_id = body.get("assignee_id", body.get("user_id"))
        if user_id:
            authorize(
                actor_for_user(actor.db, user_id), "event.read", event_instances(actor.db, event)
            )
        event.assigned_to = user_id
        details["assigned_to"] = user_id
    elif command == "close":
        closure = body.get("closure_type")
        if closure == "verified_recovery":
            verified = _valid_verification_ids(actor.db, event)
            if not episodes_recovered(actor.db, event) or not verified:
                raise PlatformError(
                    409, "recovery_not_verified", "全部告警需要新鲜恢复证据及覆盖当前范围的复验。"
                )
            details["verification_ids"] = verified
        elif closure == "manual_exception":
            if not body.get("risks"):
                raise PlatformError(422, "risks_required", "人工例外关闭需要记录未解决风险。")
            details["risks"] = body["risks"]
            details["recovery_proven"] = False
        else:
            raise PlatformError(422, "closure_type_required", "需要明确结案类型。")
        event.closure_type = closure
        event.closure_reason = reason
        state = "closed"
    elif command == "reopen":
        if event.status != "closed":
            raise PlatformError(409, "invalid_state", "只有已关闭事件可重开。")
        state = "investigating"
        event.closure_type = None
        event.closure_reason = None
        event.coverage_revision += 1
    _transition(actor.db, event, command, actor.user_id, details, to_state=state)
    if command != "comment":
        _snapshot(actor.db, event)
    append_audit(
        actor.db,
        actor.user_id,
        actions[command],
        "event",
        event.id,
        details={"version": event.version, "closure_type": event.closure_type},
    )
    actor.db.flush()
    return event_response(event)


def merge_events(actor, body):
    db = actor.db
    target_id = body.get("target_event_id")
    sources = body.get("source_event_ids", [])
    alert_ids = body.get("alert_occurrence_ids", [])
    if (
        not isinstance(target_id, str)
        or not isinstance(sources, list)
        or not isinstance(alert_ids, list)
        or any(not isinstance(value, str) for value in sources + alert_ids)
        or not isinstance(body.get("expected_versions"), dict)
    ):
        raise PlatformError(422, "invalid_merge", "归并参数无效。")
    if target_id in sources:
        raise PlatformError(422, "self_merge", "事件不能归并到自身。")
    sources = list(dict.fromkeys(sources))
    alert_ids = list(dict.fromkeys(alert_ids))
    if not sources and not alert_ids or len(sources) + len(alert_ids) > 100:
        raise PlatformError(422, "invalid_merge", "需要1–100个来源。")
    ids = sorted(set([target_id] + sources))
    events = {
        identifier: get_event(actor, identifier, "event.merge", lock=True) for identifier in ids
    }
    versions = body.get("expected_versions") or {}
    target = events[target_id]
    for event in events.values():
        check_version(event, {"expected_version": versions.get(event.id)})
        if event.status in ("closed", "merged"):
            raise PlatformError(409, "invalid_merge_state", "只能归并未关闭事件。")
    episodes = []
    for source in sources:
        episodes.extend(event_episodes(db, events[source]))
    for identifier in alert_ids:
        episode = db.get(m.AlertOccurrence, identifier)
        if not episode:
            raise PlatformError(404, "not_found", "告警不存在。")
        authorize(actor, "data.read", [episode.instance_id])
        authorize(actor, "event.merge", [episode.instance_id])
        episodes.append(episode)
    if any(event_instances(db, event) != {target.instance_id} for event in events.values()) or any(
        row.instance_id != target.instance_id for row in episodes
    ):
        raise PlatformError(409, "cross_instance_merge", "P0仅允许同实例事件归并。")
    reason = str(body.get("reason", "")).strip()
    if not reason:
        raise PlatformError(422, "reason_required", "归并需要原因。")
    current = {row.id for row in event_episodes(db, target)}
    for episode in episodes:
        if episode.id not in current:
            db.add(m.EventAlert(event_id=target.id, alert_occurrence_id=episode.id))
            current.add(episode.id)
    target.severity = _highest_severity(
        [
            target.severity,
            *[events[identifier].severity for identifier in sources],
            *[episode.severity for episode in event_episodes(db, target)],
            *[episode.severity for episode in episodes],
        ]
    )
    target.coverage_revision += 1
    _transition(
        db,
        target,
        "merge",
        actor.user_id,
        {"source_event_ids": sources, "episode_ids": sorted(current), "reason": reason},
        to_state="investigating",
    )
    for identifier in sources:
        source = events[identifier]
        source.merged_into_event_id = target.id
        _transition(
            db,
            source,
            "merged_into",
            actor.user_id,
            {"target_event_id": target.id, "reason": reason},
            to_state="merged",
        )
        # Keep every old manifest and extend its protection when merging.
        for manifest in db.scalars(
            select(m.EvidenceManifest).where(
                m.EvidenceManifest.owner_type == "event", m.EvidenceManifest.owner_id == source.id
            )
        ):
            manifest.retain_until = max(manifest.retain_until, utcnow() + timedelta(days=90))
            for pin in db.scalars(
                select(m.EvidencePin).where(m.EvidencePin.manifest_id == manifest.id)
            ):
                pin.retain_until = manifest.retain_until
    db.flush()
    _snapshot(db, target)
    append_audit(
        db,
        actor.user_id,
        "event.merge",
        "event",
        target.id,
        details={"source_event_ids": sources, "coverage_revision": target.coverage_revision},
    )
    return event_response(target)


def event_response(event):
    return fields(event, EVENT_FIELDS)


def selected_instances(actor, ids, *, department_id=None, system_id=None, environment=None):
    """Narrow a previously authorized set; selectors never add authorization."""
    if not any((department_id, system_id, environment)):
        return ids
    query = (
        select(m.Instance.id)
        .join(m.System, m.System.id == m.Instance.system_id)
        .where(m.Instance.id.in_(ids))
    )
    if department_id:
        query = query.where(m.System.department_id == department_id)
    if system_id:
        query = query.where(m.System.id == system_id)
    if environment:
        query = query.where(m.System.environment == environment)
    return set(actor.db.scalars(query))


def list_events(
    actor,
    *,
    limit=50,
    cursor=None,
    status=None,
    instance_id=None,
    department_id=None,
    system_id=None,
    environment=None,
    from_time=None,
    to_time=None,
    q=None,
    exclude_statuses=(),
    primary_instance_ids=None,
):
    ids = authorize(actor, "event.read").instance_ids & authorize(actor, "data.read").instance_ids
    query = select(m.Event).where(m.Event.instance_id.in_(ids))
    # Filter all source scopes before count and pagination, including corrupt links.
    bad_alert = exists(
        select(m.EventAlert.event_id)
        .join(m.AlertOccurrence, m.AlertOccurrence.id == m.EventAlert.alert_occurrence_id)
        .where(m.EventAlert.event_id == m.Event.id, m.AlertOccurrence.instance_id.notin_(ids))
    )
    bad_change = exists(
        select(m.ManualChange.id).where(
            m.ManualChange.event_id == m.Event.id, m.ManualChange.instance_id.notin_(ids)
        )
    )
    query = query.where(~bad_alert, ~bad_change)
    selected = selected_instances(
        actor, ids, department_id=department_id, system_id=system_id, environment=environment
    )
    query = query.where(m.Event.instance_id.in_(selected))
    if primary_instance_ids is not None:
        query = query.where(m.Event.instance_id.in_(primary_instance_ids))
    if exclude_statuses:
        query = query.where(m.Event.status.notin_(exclude_statuses))
    if status:
        query = query.where(m.Event.status == status)
    else:
        query = query.where(m.Event.status != "merged")
    if instance_id:
        authorize(actor, "event.read", [instance_id])
        query = query.where(m.Event.instance_id == instance_id)
    if from_time or to_time:
        start, end = time_window(from_time, to_time)
        query = query.where(m.Event.created_at >= start, m.Event.created_at < end)
    if q:
        escaped = q[:120].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(m.Event.id == q, m.Event.title.ilike(f"%{escaped}%", escape="\\")))
    # The detail contract includes the current frozen manifest's entire scope.
    # Check compact reference headers before count/page; filtering only primary
    # instances would leak mixed-scope historical/corrupt objects in the list.
    references = list(
        actor.db.execute(
            query.join(m.EvidenceManifest, m.EvidenceManifest.id == m.Event.manifest_id)
            .with_only_columns(m.Event.id, m.EvidenceManifest.instance_ids)
            .limit(5001)
        )
    )
    if len(references) > 5000:
        raise PlatformError(
            503, "event_scope_scan_limit", "当前查询范围过大，请缩小时间或实例范围。"
        )
    hidden = [
        identifier
        for identifier, sources in references
        if not isinstance(sources, list)
        or not all(isinstance(value, str) and value in ids for value in sources)
    ]
    if hidden:
        query = query.where(m.Event.id.notin_(hidden))
    rows, total, next_cursor = page_query(actor.db, query, m.Event, limit=limit, cursor=cursor)
    return {
        "items": [event_response(row) for row in rows],
        "total": total,
        "next_cursor": next_cursor,
    }


def event_detail(actor, event_id):
    db = actor.db
    event = get_event(actor, event_id)
    from .remediation import plan_response, verification_response

    return dict(
        event_response(event),
        episodes=[
            fields(
                row,
                [
                    "id",
                    "status",
                    "severity",
                    "first_seen_at",
                    "last_seen_at",
                    "recovered_at",
                    "evidence",
                ],
            )
            for row in event_episodes(db, event)
        ],
        timeline=[
            fields(
                row,
                ["id", "action", "actor_id", "occurred_at", "from_state", "to_state", "details"],
            )
            for row in db.scalars(
                select(m.EventTransition)
                .where(m.EventTransition.event_id == event.id)
                .order_by(m.EventTransition.occurred_at)
            )
        ],
        plans=[
            plan_response(db, row)
            for row in db.scalars(
                select(m.RemediationPlan).where(m.RemediationPlan.event_id == event.id)
            )
        ],
        verifications=[
            verification_response(row)
            for row in db.scalars(select(m.Verification).where(m.Verification.event_id == event.id))
        ],
        manual_changes=[
            fields(row, ["id", "instance_id", "event_at", "source", "description", "details"])
            for row in db.scalars(select(m.ManualChange).where(m.ManualChange.event_id == event.id))
        ],
    )
