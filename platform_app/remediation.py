"""Human plans/records and deterministic checks against frozen evidence."""

from datetime import timedelta
from sqlalchemy import select, func
from . import models as m
from .access import authorize, actor_for_user
from .audit import append_audit
from .common import PlatformError, fields, iso, json_safe, time_window
from .db import utcnow
from .evidence import create_manifest
from .events import (
    get_event,
    check_version,
    event_instances,
    event_episodes,
    episodes_recovered,
    _transition,
    _snapshot,
    _valid_verification_ids,
)
from .scheduling import enqueue


def plan_response(db, plan):
    return dict(
        fields(
            plan,
            [
                "id",
                "event_id",
                "version",
                "status",
                "source",
                "created_by",
                "created_at",
                "prerequisites",
                "risks",
                "steps",
                "rollback",
            ],
        ),
        execution_records=[
            fields(row, ["id", "executed_by", "executed_at", "source", "actual_result"])
            for row in db.scalars(
                select(m.ExecutionRecord).where(m.ExecutionRecord.plan_id == plan.id)
            )
        ],
    )


def _lines(value):
    if isinstance(value, str):
        value = [line.strip() for line in value.splitlines() if line.strip()]
    if (
        not isinstance(value, list)
        or len(value) > 50
        or any(not isinstance(item, str) or len(item) > 4000 for item in value)
    ):
        raise PlatformError(422, "invalid_plan", "计划字段须为最多50条文本。")
    return value


def create_plan(actor, event_id, body):
    db = actor.db
    event = get_event(actor, event_id, "remediation.create", lock=True)
    check_version(event, body)
    if event.status in ("closed", "merged"):
        raise PlatformError(409, "invalid_state", "事件当前状态不能新增处置计划。")
    version = (
        db.scalar(
            select(func.max(m.RemediationPlan.version)).where(
                m.RemediationPlan.event_id == event.id
            )
        )
        or 0
    ) + 1
    plan = m.RemediationPlan(
        event_id=event.id,
        created_by=actor.user_id,
        version=version,
        source="manual",
        prerequisites=_lines(body.get("prerequisites", [])),
        risks=_lines(body.get("risks", [])),
        steps=_lines(body.get("steps", [])),
        rollback=_lines(body.get("rollback", [])),
    )
    if not plan.steps:
        raise PlatformError(422, "steps_required", "需要人工处置步骤。")
    db.add(plan)
    db.flush()
    _transition(
        db,
        event,
        "plan_created",
        actor.user_id,
        {"plan_id": plan.id, "source": "human_assertion"},
        to_state="investigating",
    )
    create_manifest(
        db,
        "remediation",
        plan.id,
        [event.instance_id],
        event.created_at - timedelta(minutes=5),
        utcnow(),
    )
    append_audit(db, actor.user_id, "remediation.create", "remediation_plan", plan.id)
    return plan_response(db, plan)


def review_plan(actor, plan_id, body):
    db = actor.db
    plan = db.scalar(
        select(m.RemediationPlan).where(m.RemediationPlan.id == plan_id).with_for_update()
    )
    if not plan:
        raise PlatformError(404, "not_found", "计划不存在。")
    event = get_event(actor, plan.event_id, "remediation.review", lock=True)
    if body.get("plan_version", body.get("version")) != plan.version:
        raise PlatformError(409, "version_conflict", "计划版本已变化。")
    decision = body.get("decision")
    note = str(body.get("note", body.get("reason", ""))).strip()
    if decision not in ("approved", "rejected") or not note:
        raise PlatformError(422, "invalid_review", "需要审核结论及说明。")
    if plan.status not in ("draft", "in_review"):
        raise PlatformError(409, "invalid_state", "计划已经审核。")
    plan.status = decision
    _transition(
        db,
        event,
        "plan_review",
        actor.user_id,
        {"plan_id": plan.id, "decision": decision, "note": note, "platform_execution": False},
    )
    append_audit(
        db,
        actor.user_id,
        "remediation.review",
        "remediation_plan",
        plan.id,
        details={"decision": decision},
    )
    return plan_response(db, plan)


def record_execution(actor, plan_id, body):
    db = actor.db
    plan = db.get(m.RemediationPlan, plan_id)
    if not plan:
        raise PlatformError(404, "not_found", "计划不存在。")
    event = get_event(actor, plan.event_id, "remediation.record_execution", lock=True)
    if plan.status != "approved":
        raise PlatformError(409, "plan_not_approved", "需要先审核计划。")
    from .common import parse_time

    stamp = parse_time(body.get("executed_at"))
    if stamp > utcnow() + timedelta(seconds=5):
        raise PlatformError(422, "future_execution", "不能记录未来已执行事实。")
    result = body.get("actual_result")
    if not isinstance(result, dict) or not result:
        raise PlatformError(422, "result_required", "需要人工执行结果。")
    row = m.ExecutionRecord(
        plan_id=plan.id,
        executed_by=actor.user_id,
        executed_at=stamp,
        source="human_assertion",
        actual_result=result,
    )
    db.add(row)
    db.flush()
    _transition(
        db,
        event,
        "execution_recorded",
        actor.user_id,
        {"execution_record_id": row.id, "source": "human_assertion", "platform_execution": False},
        to_state="pending_verification" if episodes_recovered(db, event) else "investigating",
    )
    _snapshot(db, event)
    append_audit(db, actor.user_id, "remediation.record_execution", "execution_record", row.id)
    return fields(row, ["id", "plan_id", "executed_by", "executed_at", "source", "actual_result"])


def compare_locks(before, after, criteria):
    answer = {
        "outcome": "inconclusive",
        "reasons": [],
        "before_observations": len(before),
        "after_observations": len(after),
        "business_recovery_proven": False,
        "causation_proven": False,
        "load_context": criteria.get("load_context", {"kind": "unknown"}),
        "conclusion_scope": "所观测等待链是否消失；不证明业务恢复或处置因果。",
    }
    if len(before) < 3 or len(after) < 3:
        answer["reasons"] = ["insufficient_observations"]
        return answer

    def key(row):
        return tuple(
            str(row.get(name, ""))
            for name in (
                "waiting_trx_id",
                "blocking_trx_id",
                "waiting_thread_id",
                "blocking_thread_id",
            )
        )

    selected = set()
    for sample in before:
        for row in sample.get("rows", []):
            if criteria.get("blocking_trx_id") and str(row.get("blocking_trx_id")) != str(
                criteria["blocking_trx_id"]
            ):
                continue
            selected.add(key(row))
    if not selected:
        answer["reasons"] = ["baseline_wait_chain_missing"]
        return answer
    present = any(key(row) in selected for sample in after for row in sample.get("rows", []))
    answer.update(
        outcome="not_improved" if present else "improved", selected_chain_count=len(selected)
    )
    if answer["load_context"].get("kind") != "measured":
        answer["limitations"] = ["负载条件未经独立机器观测确认。"]
    return answer


def compare_sql(before, after, criteria):
    result = {
        "outcome": "inconclusive",
        "reasons": [],
        "business_recovery_proven": False,
        "causation_proven": False,
        "before_observations": before.get("observations", 0),
        "after_observations": after.get("observations", 0),
    }
    for field in ("schema", "digest", "counter_epoch", "server_uuid"):
        if before.get(field) != after.get(field):
            result["reasons"].append(field + "_changed")
    for item in (before, after):
        if item.get("observations", 0) < 3:
            result["reasons"].append("insufficient_observations")
        if item.get("executions", 0) < max(10, int(criteria.get("min_executions", 10))):
            result["reasons"].append("insufficient_executions")
        if item.get("window_seconds", 0) <= 0 or item.get("total_seconds", -1) < 0:
            result["reasons"].append("invalid_window")
        if item.get("reasons"):
            result["reasons"].extend(item["reasons"])
    if result["reasons"]:
        return result
    before_rate = before["executions"] / before["window_seconds"]
    after_rate = after["executions"] / after["window_seconds"]
    ratio = after_rate / before_rate
    result["execution_rate_ratio"] = ratio
    if not 0.5 <= ratio <= 2:
        result["reasons"] = ["incomparable_execution_rate"]
        return result
    baseline = before["total_seconds"] / before["executions"]
    current = after["total_seconds"] / after["executions"]
    if baseline <= 0:
        result["reasons"] = ["zero_baseline"]
        return result
    target = float(criteria.get("max_latency_ratio", 0.8))
    if not 0.1 <= target <= 1:
        result["reasons"] = ["invalid_latency_tolerance"]
        return result
    result.update(
        outcome="improved" if current / baseline <= target else "not_improved",
        before_avg_seconds=baseline,
        after_avg_seconds=current,
        latency_ratio=current / baseline,
        conclusion_scope="同一SQL形状在可比调用率窗口内的平均耗时变化。",
    )
    return result


def create_verification(actor, event_id, body):
    db = actor.db
    event = get_event(actor, event_id, "verification.create", lock=True)
    check_version(event, body)
    if event.status in ("closed", "merged"):
        raise PlatformError(409, "invalid_state", "已关闭事件不能启动复验。")
    template = body.get("template")
    if template not in ("locks_absent", "sql_latency"):
        raise PlatformError(422, "unsupported_template", "不支持的复验模板。")
    before = body.get("before") or {}
    after = body.get("after") or {}
    bstart, bend = time_window(before.get("from"), before.get("to"))
    astart, aend = time_window(after.get("from"), after.get("to"))
    if bend > astart or aend > utcnow():
        raise PlatformError(422, "invalid_verification_window", "前后窗口须有序且冻结于当前时刻。")
    criteria = dict(body.get("criteria") or {})
    if template == "sql_latency" and (not criteria.get("digest") or "schema" not in criteria):
        raise PlatformError(422, "digest_required", "SQL复验需要schema和digest。")
    episodes = event_episodes(db, event)
    eligible = []
    for episode in episodes:
        active = db.get(m.ActiveAlert, episode.active_alert_id)
        evaluation = db.get(m.RuleEvaluation, episode.evaluation_id)
        rule = db.get(m.Rule, evaluation.rule_id)
        if template == "locks_absent" and rule.template == "locks_present":
            eligible.append(episode)
        if template == "sql_latency" and rule.template == "sql_latency":
            trigger = episode.evidence.get("trigger") or {}
            if trigger.get("digest") == criteria.get("digest") and (
                trigger.get("schema") or ""
            ) == (criteria.get("schema") or ""):
                eligible.append(episode)
    criteria.update(
        coverage_revision=event.coverage_revision,
        episode_ids=[row.id for row in eligible],
        episode_rule_versions={
            row.id: db.get(m.ActiveAlert, row.active_alert_id).rule_version for row in eligible
        },
        load_context=criteria.get("load_context") or {"kind": "unknown"},
        minimum_observations=3,
        minimum_executions=10,
    )
    plan_id = body.get("plan_id")
    if plan_id:
        plan = db.get(m.RemediationPlan, plan_id)
        if not plan or plan.event_id != event.id:
            raise PlatformError(404, "not_found", "处置计划不存在。")
    execution_id = body.get("execution_record_id")
    if execution_id:
        execution = db.get(m.ExecutionRecord, execution_id)
        if not execution or execution.plan_id != plan_id:
            raise PlatformError(404, "not_found", "执行记录不存在。")
        criteria["execution_record_id"] = execution_id
    value = m.Verification(
        event_id=event.id,
        plan_id=plan_id,
        instance_id=event.instance_id,
        created_by=actor.user_id,
        event_version=event.version,
        before_start=bstart,
        before_end=bend,
        after_start=astart,
        after_end=aend,
        template=template,
        template_version="1",
        criteria=criteria,
        status="queued",
    )
    db.add(value)
    db.flush()
    value.manifest_id = create_manifest(
        db, "verification", value.id, event_instances(db, event), bstart, aend
    ).id
    task = enqueue(
        db,
        "verification",
        actor_id=actor.user_id,
        instance_id=event.instance_id,
        payload={"verification_id": value.id},
        max_attempts=1,
    )
    _transition(
        db,
        event,
        "verification_queued",
        actor.user_id,
        {"verification_id": value.id, "task_id": task.id},
        to_state="pending_verification" if episodes_recovered(db, event) else "investigating",
    )
    append_audit(db, actor.user_id, "verification.create", "verification", value.id)
    return task


def _window_quality(items, start, end, expected_uuid):
    valid = [item for item in items if start <= item.observed_at < end]
    valid.sort(key=lambda item: item.observed_at)
    reasons = []
    if len(valid) < 3:
        reasons.append("insufficient_observations")
    for item in valid:
        if (
            item.source_mode != "live"
            or item.coverage.get("availability") != "available"
            or item.truncated
        ):
            reasons.append("source_gap_or_truncated")
        if item.server_uuid != expected_uuid:
            reasons.append("identity_changed")
    if valid:
        interval = max(float(item.coverage.get("interval_seconds", 30)) for item in valid)
        stamps = [start] + [item.observed_at for item in valid] + [end]
        if any(
            (right - left).total_seconds() > interval * 1.5
            for left, right in zip(stamps, stamps[1:])
        ):
            reasons.append("coverage_gap")
    return valid, list(dict.fromkeys(reasons))


def _sql_summary(db, manifest_id, start, end, criteria):
    frames = list(
        db.scalars(
            select(m.MetricFrame)
            .join(m.ManifestItem, m.ManifestItem.metric_run_id == m.MetricFrame.run_id)
            .where(
                m.ManifestItem.manifest_id == manifest_id,
                m.MetricFrame.collector == "statement_digest",
                m.MetricFrame.observed_at >= start,
                m.MetricFrame.observed_at < end,
            )
            .order_by(m.MetricFrame.observed_at)
        )
    )
    result = {
        "schema": criteria.get("schema"),
        "digest": criteria.get("digest"),
        "observations": len(frames),
        "executions": 0,
        "total_seconds": 0,
        "window_seconds": 0,
        "reasons": [],
    }
    if len(frames) < 3:
        result["reasons"].append("insufficient_observations")
        return result
    signatures = {
        (f.server_uuid, f.collector_boot_id, f.source_epoch, f.counter_epoch) for f in frames
    }
    if len(signatures) != 1:
        result["reasons"].append("counter_epoch_changed")
        return result
    result.update(
        counter_epoch=str(next(iter(signatures))),
        server_uuid=frames[0].server_uuid,
        window_seconds=(frames[-1].observed_at - frames[0].observed_at).total_seconds(),
    )
    matched = []
    for frame in frames:
        row = next(
            (
                r
                for r in frame.raw_values.get("rows", [])
                if r.get("DIGEST") == criteria["digest"]
                and (r.get("SCHEMA_NAME") or "") == (criteria.get("schema") or "")
            ),
            None,
        )
        if row is None:
            result["reasons"].append("digest_missing_or_evicted")
            return result
        matched.append(row)
    for previous, current in zip(matched, matched[1:]):
        if (
            current["COUNT_STAR"] < previous["COUNT_STAR"]
            or current["SUM_TIMER_WAIT"] < previous["SUM_TIMER_WAIT"]
        ):
            result["reasons"].append("counter_decreased")
    result.update(
        executions=matched[-1]["COUNT_STAR"] - matched[0]["COUNT_STAR"],
        total_seconds=(matched[-1]["SUM_TIMER_WAIT"] - matched[0]["SUM_TIMER_WAIT"]) / 1e12,
    )
    return result


def handle_verification(db, task_or_lease):
    task = task_or_lease
    value = db.scalar(
        select(m.Verification)
        .where(m.Verification.id == (task.payload or {}).get("verification_id"))
        .with_for_update()
    )
    if not value or value.created_by != task.actor_id:
        raise PlatformError(404, "not_found", "复验不存在。")
    actor = actor_for_user(db, task.actor_id)
    event = get_event(actor, value.event_id, "verification.create", lock=True)
    if value.status == "completed":
        return value.result
    result = {
        "outcome": "inconclusive",
        "reasons": [],
        "frozen_input_valid": False,
        "before_observations": 0,
        "after_observations": 0,
    }
    if value.criteria.get("coverage_revision") != event.coverage_revision:
        result["reasons"] = ["event_coverage_changed"]
    elif not value.manifest_id:
        result["reasons"] = ["frozen_manifest_missing"]
    else:
        manifest = db.get(m.EvidenceManifest, value.manifest_id)
        authorize(actor, "data.read", manifest.instance_ids)
        items = list(
            db.scalars(
                select(m.Evidence)
                .join(m.ManifestItem, m.ManifestItem.evidence_id == m.Evidence.id)
                .where(
                    m.ManifestItem.manifest_id == manifest.id,
                    m.Evidence.instance_id == value.instance_id,
                    m.Evidence.source
                    == (
                        "mysql.lock_waits"
                        if value.template == "locks_absent"
                        else "mysql.statement_digest"
                    ),
                )
            )
        )
        instance = db.get(m.Instance, value.instance_id)
        before, bad_before = _window_quality(
            items, value.before_start, value.before_end, instance.expected_server_uuid
        )
        after, bad_after = _window_quality(
            items, value.after_start, value.after_end, instance.expected_server_uuid
        )
        coverage = list(
            db.scalars(
                select(m.ManifestItem).where(
                    m.ManifestItem.manifest_id == manifest.id,
                    m.ManifestItem.item_type == "coverage",
                )
            )
        )
        errors = (
            bad_before
            + bad_after
            + (
                ["manifest_truncated"]
                if any((item.payload or {}).get("truncated") for item in coverage)
                else []
            )
        )
        if errors:
            result.update(
                reasons=sorted(set(errors)),
                before_observations=len(before),
                after_observations=len(after),
            )
        else:
            if value.template == "locks_absent":
                result = compare_locks(
                    [item.payload for item in before],
                    [item.payload for item in after],
                    value.criteria,
                )
            else:
                result = compare_sql(
                    _sql_summary(
                        db, manifest.id, value.before_start, value.before_end, value.criteria
                    ),
                    _sql_summary(
                        db, manifest.id, value.after_start, value.after_end, value.criteria
                    ),
                    value.criteria,
                )
            result["frozen_input_valid"] = not result.get("reasons")
    result.update(
        template=value.template,
        template_version=value.template_version,
        coverage_revision=value.criteria.get("coverage_revision"),
        episode_ids=value.criteria.get("episode_ids", []),
        before={"from": iso(value.before_start), "to": iso(value.before_end)},
        after={"from": iso(value.after_start), "to": iso(value.after_end)},
        manifest_id=value.manifest_id,
    )
    value.result = json_safe(result)
    value.outcome = result["outcome"]
    value.status = "completed"
    db.flush()
    target = (
        "resolved"
        if value.outcome == "improved"
        and episodes_recovered(db, event)
        and _valid_verification_ids(db, event)
        else "investigating"
        if value.outcome == "not_improved"
        else "pending_verification"
    )
    _transition(
        db,
        event,
        "verification_completed",
        actor.user_id,
        {"verification_id": value.id, "outcome": value.outcome},
        to_state=target,
    )
    append_audit(
        db,
        actor.user_id,
        "verification.completed",
        "verification",
        value.id,
        details={"outcome": value.outcome},
    )
    return value.result


def verification_response(value):
    return fields(
        value,
        [
            "id",
            "event_id",
            "plan_id",
            "instance_id",
            "created_by",
            "event_version",
            "status",
            "template",
            "template_version",
            "criteria",
            "before_start",
            "before_end",
            "after_start",
            "after_end",
            "outcome",
            "result",
            "manifest_id",
            "created_at",
        ],
    )
