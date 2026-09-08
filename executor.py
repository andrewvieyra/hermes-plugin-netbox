"""Apply a plan step by step with a persisted journal, and roll a plan back from that journal.

Guarantees the executor enforces on every run:

* A plan applies at most once (``planned -> applying`` is flipped under the store lock).
* Every ``update``/``delete`` step re-reads its object and refuses to run when ``last_updated``
  differs from what the planner saw; a ``create`` with a ``match`` refuses to run when a match now
  exists. A conflict stops the run exactly like an error does.
* Each completed step records its inverse (delete the created object / PATCH the previous values /
  re-create from snapshot) and the object's post-write ``last_updated`` before the next step starts.
* On failure the completed steps are reverted in reverse order (unless ``rollback_on_failure`` is off),
  and the outcome of every revert is journaled too.
* Rollback later checks the same ``last_updated`` stamps so it never clobbers a change somebody
  made after the apply; ``force=True`` overrides that check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from . import audit, timefmt
from .client import NetBoxClient, NetBoxError
from .diff import format_value, snapshot_to_payload
from .settings import Settings
from .store import PlanStore, now_iso

APPLYABLE = ("planned",)
ROLLBACKABLE = ("applied", "failed", "partially_rolled_back")


class ExecutionRefused(Exception):
    """Preflight rejected the request (wrong status, stale plan, other NetBox, deletes disabled ...)."""


def _parse_iso(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except Exception:
        return None


def _stamp_of(obj: Dict[str, Any] | None) -> str | None:
    return obj.get("last_updated") if isinstance(obj, dict) else None


def _emit(event: str, plan: Dict[str, Any], actor: Dict[str, Any] | None, **details: Any) -> None:
    audit.emit(event, plan_id=plan.get("id"), actor=actor, netbox_url=plan.get("netbox_url"), **details)


def _step_fields(step: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": step["index"],
        "action": step["action"],
        "endpoint": step["endpoint"],
        "object_id": step.get("object_id"),
        "label": step.get("label"),
    }


# -- preflight -----------------------------------------------------------------------------------


def preflight_apply(plan: Dict[str, Any], client: NetBoxClient, settings: Settings) -> None:
    if plan.get("status") not in APPLYABLE:
        raise ExecutionRefused(
            f"plan {plan['id']} is {plan.get('status')}; only a 'planned' plan can be applied "
            "(build a new plan with netbox_plan)"
        )
    created = _parse_iso(plan.get("created_at", ""))
    if created is None:
        raise ExecutionRefused("plan has no valid created_at stamp")
    age = datetime.now(timezone.utc) - created
    if age > timedelta(hours=settings.max_plan_age_hours):
        raise ExecutionRefused(
            f"plan is {age.total_seconds() / 3600:.1f}h old; max_plan_age_hours is "
            f"{settings.max_plan_age_hours}. Re-run netbox_plan."
        )
    if plan.get("netbox_url") and plan["netbox_url"].rstrip("/") != client.base_url:
        raise ExecutionRefused(f"plan was built against {plan['netbox_url']} but NETBOX_URL is now {client.base_url}")
    if not settings.allow_delete and any(s["action"] == "delete" for s in plan["steps"]):
        raise ExecutionRefused("plan contains delete steps but allow_delete is false")


def check_step_precondition(client: NetBoxClient, step: Dict[str, Any]) -> str | None:
    """Return a conflict message when the world no longer matches what the planner saw."""
    action = step["action"]
    if action == "noop":
        return None
    if action == "create":
        exists = step.get("exists_check")
        if exists:
            count, _ = client.find(step["endpoint"], exists)
            if count:
                return f"{count} {step['endpoint']} object(s) now match {format_value(exists)}; expected none"
        return None
    obj = client.get_object(step["endpoint"], step["object_id"])
    if obj is None:
        return f"{step['endpoint']} #{step['object_id']} no longer exists"
    expected = (step.get("precondition") or {}).get("last_updated")
    if expected and _stamp_of(obj) != expected:
        return (
            f"{step['endpoint']} #{step['object_id']} changed since planning "
            f"(last_updated {expected} -> {_stamp_of(obj)})"
        )
    return None


def check_preconditions(client: NetBoxClient, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    conflicts = []
    for step in plan["steps"]:
        try:
            message = check_step_precondition(client, step)
        except NetBoxError as exc:
            message = f"precondition check failed: {exc}"
        if message:
            conflicts.append({"index": step["index"], "error": message})
    return conflicts


# -- apply ----------------------------------------------------------------------------------------


def _execute_step(client: NetBoxClient, step: Dict[str, Any]) -> Dict[str, Any]:
    """Perform one step; return the journal entry (without status bookkeeping)."""
    endpoint = step["endpoint"]
    action = step["action"]
    entry: Dict[str, Any] = {
        "index": step["index"],
        "action": action,
        "endpoint": endpoint,
        "object_id": step.get("object_id"),
        "label": step.get("label"),
    }
    if action == "create":
        created = client.create(endpoint, step["data"])
        entry.update(
            object_id=created["id"],
            after_last_updated=_stamp_of(created),
            inverse={"action": "delete", "endpoint": endpoint, "object_id": created["id"]},
        )
    elif action == "update":
        updated = client.update(endpoint, step["object_id"], step["payload"])
        previous = {field: change["from"] for field, change in step["changes"].items()}
        entry.update(
            after_last_updated=_stamp_of(updated),
            inverse={"action": "update", "endpoint": endpoint, "object_id": step["object_id"], "data": previous},
        )
    elif action == "delete":
        client.delete(endpoint, step["object_id"])
        entry.update(inverse={"action": "create", "endpoint": endpoint, "snapshot": step["snapshot"]})
    return entry


def apply_plan(
    client: NetBoxClient,
    plan: Dict[str, Any],
    store: PlanStore,
    settings: Settings,
    *,
    rollback_on_failure: bool = True,
    dry_run: bool = False,
    actor: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Apply *plan* in order. Mutates and persists *plan*; returns it. Raises ExecutionRefused on preflight."""
    preflight_apply(plan, client, settings)
    if dry_run:
        conflicts = check_preconditions(client, plan)
        plan.setdefault("checks", []).append({"at": now_iso(), "conflicts": conflicts, "actor": actor})
        store.save(plan)
        _emit("plan_checked", plan, actor, conflicts=len(conflicts), applicable=not conflicts)
        return plan

    # Claim the plan under the cross-process lock so a concurrent apply of the same id (gateway vs CLI)
    # is refused rather than run twice.
    try:
        with store.exclusive(timeout=store.claim_timeout):
            current = store.load(plan["id"]) or plan
            if current.get("status") != "planned":
                raise ExecutionRefused(f"plan {plan['id']} is {current.get('status')}; refusing to apply twice")
            plan.update(current)
            plan["status"] = "applying"
            plan["journal"] = []
            plan["apply"] = {"started_at": now_iso(), "rollback_on_failure": rollback_on_failure, "actor": actor}
            store.save(plan)
    except TimeoutError as exc:
        raise ExecutionRefused(f"{exc}; another apply or rollback is in progress, try again shortly") from exc
    _emit(
        "apply_started",
        plan,
        actor,
        steps=len(plan["steps"]),
        summary=plan.get("summary"),
        rollback_on_failure=rollback_on_failure,
    )

    failure: Dict[str, Any] | None = None
    for step in plan["steps"]:
        if step["action"] == "noop":
            plan["journal"].append(
                {
                    "index": step["index"],
                    "action": "noop",
                    "endpoint": step["endpoint"],
                    "object_id": step.get("object_id"),
                    "label": step.get("label"),
                    "status": "skipped",
                }
            )
            store.save(plan)
            _emit("step_skipped", plan, actor, **_step_fields(step), status="skipped")
            continue
        try:
            conflict = check_step_precondition(client, step)
        except NetBoxError as exc:
            conflict = f"precondition check failed: {exc}"
        if conflict:
            plan["journal"].append(
                {
                    "index": step["index"],
                    "action": step["action"],
                    "endpoint": step["endpoint"],
                    "object_id": step.get("object_id"),
                    "label": step.get("label"),
                    "status": "conflict",
                    "error": conflict,
                }
            )
            failure = {"index": step["index"], "kind": "conflict", "error": conflict}
            store.save(plan)
            _emit("step_conflict", plan, actor, **_step_fields(step), status="conflict", error=conflict)
            break
        try:
            entry = _execute_step(client, step)
        except NetBoxError as exc:
            plan["journal"].append(
                {
                    "index": step["index"],
                    "action": step["action"],
                    "endpoint": step["endpoint"],
                    "object_id": step.get("object_id"),
                    "label": step.get("label"),
                    "status": "failed",
                    "error": str(exc),
                    "netbox": exc.to_dict(),
                    "request": dict(client.last_call),
                }
            )
            failure = {"index": step["index"], "kind": "error", "error": str(exc)}
            store.save(plan)
            _emit(
                "step_failed",
                plan,
                actor,
                **_step_fields(step),
                status="failed",
                error=str(exc),
                http_status=exc.status,
                request=dict(client.last_call),
            )
            break
        entry["status"] = "done"
        entry["at"] = now_iso()
        entry["request"] = dict(client.last_call)
        plan["journal"].append(entry)
        store.save(plan)
        _emit(
            "step_done",
            plan,
            actor,
            **{**_step_fields(step), "object_id": entry.get("object_id")},  # a create learns its id here
            status="done",
            http_status=entry["request"].get("status"),
            request=entry["request"],
        )

    plan["apply"]["finished_at"] = now_iso()
    if failure is None:
        plan["status"] = "applied"
        plan["apply"]["outcome"] = "applied"
        store.save(plan)
        _emit(
            "apply_finished",
            plan,
            actor,
            outcome="applied",
            status=plan["status"],
            done=sum(1 for e in plan["journal"] if e.get("status") == "done"),
        )
        return plan

    plan["status"] = "failed"
    plan["apply"].update(outcome="failed", failed_step=failure["index"], failure=failure)
    store.save(plan)
    _emit(
        "apply_finished",
        plan,
        actor,
        outcome="failed",
        status=plan["status"],
        failed_step=failure["index"],
        failure_kind=failure["kind"],
        error=failure["error"],
        done=sum(1 for e in plan["journal"] if e.get("status") == "done"),
    )
    if rollback_on_failure and any(e.get("status") == "done" for e in plan["journal"]):
        rollback_plan(
            client, plan, store, force=False, reason=f"automatic after failure at step {failure['index']}", actor=actor
        )
    return plan


# -- rollback -------------------------------------------------------------------------------------


def _revert_entry(client: NetBoxClient, entry: Dict[str, Any], force: bool) -> Tuple[str, Dict[str, Any]]:
    """Execute the inverse of a journal entry. Returns ``(status, details)``; status is
    ``reverted`` | ``conflict`` | ``failed``."""
    inverse = entry.get("inverse") or {}
    kind = inverse.get("action")
    endpoint = inverse.get("endpoint")
    if kind == "delete":  # undo create
        current = client.get_object(endpoint, inverse["object_id"])
        if current is None:
            return "reverted", {"note": "object already gone"}
        expected = entry.get("after_last_updated")
        if not force and expected and _stamp_of(current) != expected:
            return "conflict", {
                "error": f"{endpoint} #{inverse['object_id']} was modified after apply "
                f"({expected} -> {_stamp_of(current)}); use force to delete anyway"
            }
        client.delete(endpoint, inverse["object_id"])
        return "reverted", {}
    if kind == "update":  # undo update
        current = client.get_object(endpoint, inverse["object_id"])
        if current is None:
            return "failed", {"error": f"{endpoint} #{inverse['object_id']} no longer exists; cannot restore fields"}
        expected = entry.get("after_last_updated")
        if not force and expected and _stamp_of(current) != expected:
            return "conflict", {
                "error": f"{endpoint} #{inverse['object_id']} was modified after apply "
                f"({expected} -> {_stamp_of(current)}); use force to overwrite"
            }
        client.update(endpoint, inverse["object_id"], inverse["data"])
        return "reverted", {"restored_fields": sorted(inverse["data"])}
    if kind == "create":  # undo delete
        payload = snapshot_to_payload(inverse["snapshot"])
        dropped: List[str] = []
        try:
            created = client.create(endpoint, payload)
        except NetBoxError as exc:
            bad = [f for f in exc.field_errors() if f in payload]
            if exc.status != 400 or not bad:
                raise
            dropped = bad  # e.g. primary_ip4 referencing an IP that was cascade-deleted
            created = client.create(endpoint, snapshot_to_payload(inverse["snapshot"], drop=bad))
        return "reverted", {
            "new_object_id": created["id"],
            "dropped_fields": dropped,
            "note": "re-created with a new id; objects that cascaded on delete are not restored",
        }
    return "failed", {"error": f"unknown inverse action {kind!r}"}


def rollback_plan(
    client: NetBoxClient,
    plan: Dict[str, Any],
    store: PlanStore,
    *,
    force: bool = False,
    reason: str = "",
    actor: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Revert every ``done`` journal entry in reverse order. Mutates and persists *plan*; returns it."""
    if plan.get("status") not in ROLLBACKABLE:
        raise ExecutionRefused(
            f"plan {plan['id']} is {plan.get('status')}; rollback needs one of {', '.join(ROLLBACKABLE)}"
        )
    if plan.get("netbox_url") and plan["netbox_url"].rstrip("/") != client.base_url:
        raise ExecutionRefused(f"plan was applied against {plan['netbox_url']} but NETBOX_URL is now {client.base_url}")
    try:
        with store.exclusive(timeout=store.claim_timeout):
            current = store.load(plan["id"]) or plan
            if current.get("status") not in ROLLBACKABLE:
                raise ExecutionRefused(
                    f"plan {plan['id']} is {current.get('status')}; rollback needs one of {', '.join(ROLLBACKABLE)}"
                )
            plan.update(current)
            pending = [
                e for e in plan.get("journal", []) if e.get("status") == "done" and e.get("revert_status") != "reverted"
            ]
            plan["status"] = "rolling_back"
            plan["rollback"] = {
                "started_at": now_iso(),
                "force": force,
                "reason": reason,
                "entries": len(pending),
                "actor": actor,
            }
            store.save(plan)
    except TimeoutError as exc:
        raise ExecutionRefused(f"{exc}; another apply or rollback is in progress, try again shortly") from exc
    _emit("rollback_started", plan, actor, entries=len(pending), force=force, reason=reason)

    counts = {"reverted": 0, "conflict": 0, "failed": 0}
    for entry in reversed(pending):
        try:
            status, details = _revert_entry(client, entry, force)
        except NetBoxError as exc:
            status, details = "failed", {"error": str(exc), "netbox": exc.to_dict()}
        entry["revert_status"] = status
        entry["revert_at"] = now_iso()
        entry["revert"] = details
        entry["revert_request"] = dict(client.last_call)
        counts[status] += 1
        store.save(plan)
        _emit(
            f"revert_{status}",
            plan,
            actor,
            index=entry.get("index"),
            action=entry.get("action"),
            endpoint=entry.get("endpoint"),
            object_id=entry.get("object_id"),
            label=entry.get("label"),
            status=status,
            error=details.get("error"),
            new_object_id=details.get("new_object_id"),
            http_status=client.last_call.get("status"),
            request=dict(client.last_call),
        )

    plan["rollback"].update(finished_at=now_iso(), **counts)
    plan["status"] = "rolled_back" if counts["conflict"] == 0 and counts["failed"] == 0 else "partially_rolled_back"
    if not pending:
        plan["status"] = "rolled_back"
    store.save(plan)
    _emit("rollback_finished", plan, actor, status=plan["status"], force=force, reason=reason, **counts)
    return plan


# -- rendering ------------------------------------------------------------------------------------


def render_journal(plan: Dict[str, Any]) -> str:
    lines = [f"Plan {plan['id']} ({plan['status']})"]
    apply = plan.get("apply") or {}
    if apply.get("started_at"):
        lines.append(f"Applied: {timefmt.local(apply['started_at'])} by {audit.describe_actor(apply.get('actor'))}")
    if apply.get("outcome") == "failed":
        f = apply.get("failure", {})
        lines.append(f"Apply FAILED at step {f.get('index')}: {f.get('error')}")
    for entry in plan.get("journal", []):
        ident = f"#{entry['object_id']}" if entry.get("object_id") is not None else ""
        line = f"[{entry['index']}] {entry.get('action', ''):<6} {entry.get('endpoint', '')} {ident} -> {entry.get('status')}"
        if entry.get("error"):
            line += f": {entry['error']}"
        if entry.get("revert_status"):
            line += f" | rollback: {entry['revert_status']}"
            details = entry.get("revert") or {}
            if details.get("error"):
                line += f" ({details['error']})"
            elif details.get("new_object_id"):
                line += f" (re-created as #{details['new_object_id']})"
        lines.append(line)
    rb = plan.get("rollback")
    if rb and rb.get("started_at"):
        lines.append(f"Rolled back: {timefmt.local(rb['started_at'])} by {audit.describe_actor(rb.get('actor'))}")
    if rb and "reverted" in rb:
        lines.append(
            f"Rollback: {rb['reverted']} reverted, {rb['conflict']} conflicts, {rb['failed']} failed"
            + (f" ({rb['reason']})" if rb.get("reason") else "")
        )
    return "\n".join(lines)
