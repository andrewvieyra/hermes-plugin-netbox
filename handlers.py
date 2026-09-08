"""Tool handlers: ``handler(args: dict, **kwargs) -> str`` (always a JSON string, never raises).

Handlers are thin. They parse arguments, obtain a client and the plan store, call the planner or
executor, and shape the result. Everything here is also reachable from the ``/netbox`` slash command
and the ``hermes netbox`` CLI through :mod:`commands`.

Named ``handlers`` rather than ``tools`` so the module can never shadow Hermes' own ``tools`` package
when the repository root is on ``sys.path`` (tests, editable checkouts).
"""

from __future__ import annotations

import contextlib
import functools
import json
from typing import Any, Callable, Dict, List

from . import audit
from .client import NetBoxClient, NetBoxError, is_configured, validate_endpoint
from .executor import ExecutionRefused, apply_plan, render_journal, rollback_plan
from .planner import PlanError, build_plan, render_plan
from .settings import get_settings
from .store import get_store
from .version import __version__

_client_factory: Callable[[], NetBoxClient] = NetBoxClient.from_env


def set_client_factory(factory: Callable[[], NetBoxClient] | None) -> None:
    """Test seam: swap how handlers obtain a client."""
    global _client_factory
    _client_factory = factory or NetBoxClient.from_env


def check_requirements() -> bool:
    """``check_fn`` for registration: tools are exposed only when NETBOX_URL and NETBOX_TOKEN are set."""
    return is_configured()


def check_write_requirements() -> bool:
    """``check_fn`` for netbox_apply / netbox_rollback: hidden entirely when write_mode is read_only."""
    return is_configured() and get_settings().write_mode != "read_only"


def write_gate(actor: Dict[str, Any], action: str, plan_id: str, *, dry_run: bool = False) -> str | None:
    """Enforce ``write_mode``. Returns a refusal message, or None when the write may proceed.
    ``dry_run`` never writes and is always allowed."""
    if dry_run:
        return None
    mode = get_settings().write_mode
    if mode == "read_only":
        return f"{action} refused: write_mode is read_only on this install; queries and plans still work"
    if mode == "operator_only" and actor.get("kind") != "operator":
        return (
            f"{action} refused: write_mode is operator_only, so the model cannot write. Ask the user to run "
            f"`/netbox {action} {plan_id}` in this session or `hermes netbox {action} {plan_id}` from a shell."
        )
    return None


def journal_summary(journal: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-entry summary for tool output (no snapshots or inverse payloads; netbox_plans returns those)."""
    out = []
    for e in journal:
        item = {
            "index": e.get("index"),
            "action": e.get("action"),
            "endpoint": e.get("endpoint"),
            "object_id": e.get("object_id"),
            "label": e.get("label"),
            "status": e.get("status"),
            "http_status": (e.get("request") or {}).get("status"),
        }
        if e.get("error"):
            item["error"] = e["error"]
        if e.get("revert_status"):
            item["revert_status"] = e["revert_status"]
            revert = e.get("revert") or {}
            if revert.get("error"):
                item["revert_error"] = revert["error"]
            if revert.get("new_object_id"):
                item["new_object_id"] = revert["new_object_id"]
        out.append(item)
    return out


_last_prune_at: float | None = None  # None = never pruned in this process (0.0 is a valid monotonic value)
_PRUNE_INTERVAL = 3600.0


def prune_plans(
    *, days: int | None = None, dry_run: bool = False, actor: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """Remove plans older than *days* (default ``plan_retention_days``); audited as ``plans_pruned``."""
    max_age = get_settings().plan_retention_days if days is None else int(days)
    removed = get_store().prune(max_age, dry_run=dry_run)
    if removed and not dry_run:
        audit.emit(
            "plans_pruned",
            plan_id=None,
            actor=actor,
            netbox_url=None,
            max_age_days=max_age,
            count=len(removed),
            plan_ids=[r["id"] for r in removed],
        )
    return {"max_age_days": max_age, "dry_run": dry_run, "removed": removed}


def _maybe_prune(actor: Dict[str, Any]) -> None:
    """Opportunistic retention sweep, at most once per process per hour; never raises."""
    global _last_prune_at
    import time

    if get_settings().plan_retention_days <= 0:
        return
    if _last_prune_at is not None and time.monotonic() - _last_prune_at < _PRUNE_INTERVAL:
        return
    _last_prune_at = time.monotonic()
    with contextlib.suppress(Exception):  # retention is housekeeping; a failure must not affect planning
        prune_plans(actor=actor)


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(**payload: Any) -> str:
    return _json({"success": True, **payload})


def _fail(message: str, **extra: Any) -> str:
    return _json({"success": False, "error": message, **extra})


def guarded(fn: Callable[..., str]) -> Callable[..., str]:
    """Convert every exception class the handler can meet into a structured error result."""

    @functools.wraps(fn)
    def wrapper(args: Dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            return fn(dict(args or {}), **kwargs)
        except PlanError as exc:
            return _fail("plan not created: fix the listed operations and re-plan", errors=exc.errors)
        except ExecutionRefused as exc:
            return _fail(str(exc), refused=True)
        except NetBoxError as exc:
            return _fail(str(exc), netbox=exc.to_dict())
        except Exception as exc:  # never let a handler raise into the agent loop
            return _fail(f"{type(exc).__name__}: {exc}")

    return wrapper


def _int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    number = max(minimum, number)
    return min(number, maximum) if maximum else number


def _bool(value: Any, default: bool) -> bool:
    """Tolerant boolean: models occasionally send "false"/"no" as strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _actor_from(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Operator paths (slash command, CLI) pass a ready-made ``_actor``; a model tool call gets one captured
    from Hermes' session context plus the ``task_id`` / ``session_id`` / ``user_task`` kwargs it passes."""
    given = kwargs.get("_actor")
    return dict(given) if isinstance(given, dict) else audit.capture_actor(kwargs, via=audit.VIA_MODEL)


def _plan_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "plan_id": plan["id"],
        "status": plan["status"],
        "description": plan.get("description", ""),
        "created_at": plan.get("created_at"),
        "summary": plan.get("summary", {}),
        "netbox_url": plan.get("netbox_url"),
    }


# -- handlers -----------------------------------------------------------------------------------


@guarded
def netbox_query(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    settings = get_settings()
    raw_endpoint = str(args.get("endpoint") or "").strip().strip("/")
    if raw_endpoint == "status":
        return _ok(status=client.status(), plugin_version=__version__, write_mode=get_settings().write_mode)
    endpoint = validate_endpoint(raw_endpoint)
    params: Dict[str, Any] = dict(args.get("filters") or {})
    fields = args.get("fields")
    if isinstance(fields, list) and fields:
        params["fields"] = ",".join(str(f) for f in fields)
    if _bool(args.get("brief"), False):
        params["brief"] = 1
    if args.get("id") is not None:
        obj = client.get_object(endpoint, int(args["id"]))
        if obj is None:
            return _fail(f"{endpoint} #{args['id']} not found", status=404)
        if params:  # detail endpoints honour fields/brief too
            obj = client.request("GET", f"{endpoint}/{int(args['id'])}", params=params) or obj
        return _ok(endpoint=endpoint, object=obj)
    limit = _int(args.get("limit"), 50, maximum=settings.max_query_results)
    total, results = client.list(endpoint, params, max_results=limit)
    return _ok(endpoint=endpoint, count=total, returned=len(results), truncated=total > len(results), results=results)


@guarded
def netbox_plan(args: Dict[str, Any], **kwargs: Any) -> str:
    client = _client_factory()
    settings = get_settings()
    actor = _actor_from(kwargs)
    description = str(args.get("description") or "")
    try:
        plan = build_plan(client, args.get("operations"), description, settings, actor=actor)
    except PlanError as exc:
        audit.emit(
            "plan_rejected",
            plan_id=None,
            actor=actor,
            netbox_url=client.base_url,
            description=description,
            operations=len(args.get("operations") or []) if isinstance(args.get("operations"), list) else None,
            errors=len(exc.errors),
            first_error=exc.errors[0].get("error") if exc.errors else None,
        )
        raise
    get_store().create(plan)
    _maybe_prune(actor)
    audit.emit(
        "plan_created",
        plan_id=plan["id"],
        actor=actor,
        netbox_url=plan["netbox_url"],
        description=plan["description"],
        summary=plan["summary"],
        steps=len(plan["steps"]),
        warnings=len(plan["warnings"]),
    )
    changes = sum(plan["summary"].get(a, 0) for a in ("create", "update", "delete"))
    return _ok(
        **_plan_summary(plan),
        diff=render_plan(plan),
        steps=plan["steps"],
        warnings=plan["warnings"],
        next_step=(
            "Nothing to apply: every operation already matches NetBox."
            if changes == 0
            else "Show this diff to the user and call netbox_apply only after they confirm."
        ),
    )


@guarded
def netbox_apply(args: Dict[str, Any], **kwargs: Any) -> str:
    plan_id = str(args.get("plan_id") or "").strip()
    store = get_store()
    actor = _actor_from(kwargs)
    plan = store.load(plan_id)
    if plan is None:
        audit.emit("apply_refused", plan_id=plan_id or None, actor=actor, netbox_url=None, reason="unknown plan_id")
        return _fail(f"unknown plan_id {plan_id!r}")
    dry_run = _bool(args.get("dry_run"), False)
    refusal = write_gate(actor, "apply", plan["id"], dry_run=dry_run)
    if refusal:
        audit.emit(
            "apply_refused",
            plan_id=plan["id"],
            actor=actor,
            netbox_url=plan.get("netbox_url"),
            reason=refusal,
            dry_run=dry_run,
            write_mode=get_settings().write_mode,
        )
        return _fail(refusal, refused=True, write_mode=get_settings().write_mode)
    client = _client_factory()
    rollback = _bool(args.get("rollback_on_failure"), True)
    try:
        plan = apply_plan(
            client, plan, store, get_settings(), rollback_on_failure=rollback, dry_run=dry_run, actor=actor
        )
    except ExecutionRefused as exc:
        audit.emit(
            "apply_refused",
            plan_id=plan["id"],
            actor=actor,
            netbox_url=plan.get("netbox_url"),
            reason=str(exc),
            dry_run=dry_run,
        )
        raise
    if dry_run:
        conflicts = plan["checks"][-1]["conflicts"]
        return _ok(**_plan_summary(plan), dry_run=True, applicable=not conflicts, conflicts=conflicts)
    outcome = plan.get("apply", {}).get("outcome")
    return _json(
        {
            "success": outcome == "applied",
            **_plan_summary(plan),
            "outcome": outcome,
            "failure": plan.get("apply", {}).get("failure"),
            "rollback": _rollback_summary(plan.get("rollback")),
            "journal": journal_summary(plan["journal"]),
            "report": render_journal(plan),
        }
    )


def _rollback_summary(rb: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not rb:
        return None
    return {
        k: rb.get(k)
        for k in ("started_at", "finished_at", "reason", "force", "entries", "reverted", "conflict", "failed")
    }


@guarded
def netbox_rollback(args: Dict[str, Any], **kwargs: Any) -> str:
    plan_id = str(args.get("plan_id") or "").strip()
    store = get_store()
    actor = _actor_from(kwargs)
    plan = store.load(plan_id)
    if plan is None:
        audit.emit("rollback_refused", plan_id=plan_id or None, actor=actor, netbox_url=None, reason="unknown plan_id")
        return _fail(f"unknown plan_id {plan_id!r}")
    refusal = write_gate(actor, "rollback", plan["id"])
    if refusal:
        audit.emit(
            "rollback_refused",
            plan_id=plan["id"],
            actor=actor,
            netbox_url=plan.get("netbox_url"),
            reason=refusal,
            write_mode=get_settings().write_mode,
        )
        return _fail(refusal, refused=True, write_mode=get_settings().write_mode)
    client = _client_factory()
    force = _bool(args.get("force"), False)
    try:
        plan = rollback_plan(client, plan, store, force=force, reason="requested", actor=actor)
    except ExecutionRefused as exc:
        audit.emit(
            "rollback_refused",
            plan_id=plan["id"],
            actor=actor,
            netbox_url=plan.get("netbox_url"),
            reason=str(exc),
            force=force,
        )
        raise
    rb = plan.get("rollback", {})
    return _json(
        {
            "success": plan["status"] == "rolled_back",
            **_plan_summary(plan),
            "rollback": _rollback_summary(rb),
            "journal": journal_summary(plan["journal"]),
            "report": render_journal(plan),
        }
    )


@guarded
def netbox_plans(args: Dict[str, Any], **_: Any) -> str:
    store = get_store()
    plan_id = str(args.get("plan_id") or "").strip()
    if plan_id:
        plan = store.load(plan_id)
        if plan is None:
            return _fail(f"unknown plan_id {plan_id!r}")
        return _ok(
            **_plan_summary(plan),
            diff=render_plan(plan),
            steps=plan["steps"],
            journal=plan.get("journal", []),
            apply=plan.get("apply"),
            rollback=plan.get("rollback"),
            report=render_journal(plan),
        )
    limit = _int(args.get("limit"), 20, maximum=200)
    plans = store.list(status=args.get("status") or None, limit=limit)
    return _ok(count=len(plans), plans=[_plan_summary(p) for p in plans])


HANDLERS = {
    "netbox_query": netbox_query,
    "netbox_plan": netbox_plan,
    "netbox_apply": netbox_apply,
    "netbox_rollback": netbox_rollback,
    "netbox_plans": netbox_plans,
}
