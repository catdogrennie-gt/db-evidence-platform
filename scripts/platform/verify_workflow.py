"""Deliberate Task-5 acceptance against an already observed, isolated incident.

This command does not inject faults, execute SQL remediation, fabricate samples
or send notifications. `queue` freezes existing evidence; later data cannot make
the submitted verification complete. Run help for reproducible invocations.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from platform_app import models as m
from platform_app.access import actor_for_user
from platform_app.common import PlatformError, json_safe
from platform_app.config import load_config
from platform_app.db import create_db_engine, create_session_factory
from platform_app.events import event_detail, event_command, get_event
from platform_app.knowledge import create_entry, submit_review, review_entry
from platform_app.remediation import create_verification, handle_verification
from platform_app.scheduling import task_ref


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Private runtime config; defaults to DBA_PLATFORM_CONFIG")
    parser.add_argument(
        "--actor", required=True, help="Existing, explicitly scoped acceptance operator username"
    )
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--output", help="Optional private JSON report destination")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect", help="Read event facts, episodes and previous verification results")
    queue = sub.add_parser(
        "queue", help="Queue verification using frozen, already collected before/after windows"
    )
    queue.add_argument("--template", choices=["locks_absent", "sql_latency"], required=True)
    for name in ("before-from", "before-to", "after-from", "after-to"):
        queue.add_argument("--" + name, required=True)
    queue.add_argument("--schema")
    queue.add_argument("--digest")
    queue.add_argument("--blocking-trx-id")
    queue.add_argument(
        "--load-kind", choices=["measured", "human_assertion", "unknown"], default="unknown"
    )
    queue.add_argument(
        "--load-description", default="No independent workload measurement provided."
    )
    queue.add_argument(
        "--inline",
        action="store_true",
        help="Run deterministic handler directly; explicitly does not verify worker dispatch",
    )
    close = sub.add_parser(
        "close", help="Deliberately close only when all episodes and current-scope checks pass"
    )
    close.add_argument("--reason", required=True)
    knowledge = sub.add_parser(
        "knowledge", help="Create, submit and manually review a source-scoped knowledge version"
    )
    knowledge.add_argument("--title", required=True)
    knowledge.add_argument("--body-file", required=True)
    knowledge.add_argument("--review-note", required=True)
    knowledge.add_argument("--verification-id")
    args = parser.parse_args()
    config = load_config(args.config)
    engine = create_db_engine(config)
    with create_session_factory(engine, config)() as db:
        user = db.scalar(select(m.User).where(m.User.username == args.actor, m.User.active == True))
        if not user:
            raise PlatformError(404, "not_found", "Acceptance operator does not exist.")
        actor = actor_for_user(db, user.id)
        event = get_event(actor, args.event_id)
        if args.command == "inspect":
            result = event_detail(actor, event.id)
        elif args.command == "queue":
            criteria = {
                "load_context": {"kind": args.load_kind, "description": args.load_description}
            }
            if args.schema is not None:
                criteria["schema"] = args.schema
            if args.digest:
                criteria["digest"] = args.digest
            if args.blocking_trx_id:
                criteria["blocking_trx_id"] = args.blocking_trx_id
            task = create_verification(
                actor,
                event.id,
                {
                    "expected_version": event.version,
                    "template": args.template,
                    "before": {"from": args.before_from, "to": args.before_to},
                    "after": {"from": args.after_from, "to": args.after_to},
                    "criteria": criteria,
                },
            )
            db.commit()
            result = dict(
                task_ref(task),
                verification_id=task.payload["verification_id"],
                mode="queued_worker_verification",
            )
            if args.inline:
                result["verification"] = handle_verification(db, task)
                task.status = "succeeded"
                task.result = result["verification"]
                task.result_ref = "/verifications/" + task.payload["verification_id"]
                db.commit()
                result["mode"] = "direct_handler_validation_worker_dispatch_not_tested"
        elif args.command == "close":
            result = event_command(
                actor,
                event.id,
                "close",
                {
                    "expected_version": event.version,
                    "closure_type": "verified_recovery",
                    "reason": args.reason,
                },
            )
            db.commit()
        else:
            body = Path(args.body_file).read_text(encoding="utf-8-sig")
            result = create_entry(
                actor,
                {
                    "title": args.title,
                    "source_event_id": event.id,
                    "verification_id": args.verification_id,
                    "instance_ids": [event.instance_id],
                    "content": {"body": body, "acceptance_evidence": True},
                    "tags": ["isolated-acceptance"],
                },
            )
            submit_review(actor, result["id"], {"version": 1})
            result = review_entry(
                actor,
                result["id"],
                {"version": 1, "decision": "published", "note": args.review_note},
            )
            db.commit()
    engine.dispose()
    encoded = json.dumps(json_safe(result), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
