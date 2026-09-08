"""Tool handlers: ``handler(args: dict, **kwargs) -> str`` (always a JSON string, never raises).

Handlers are thin. They parse arguments, obtain a client and the plan store, call the planner or
executor, and shape the result. Everything here is also reachable from the ``/netbox`` slash command
and the ``hermes netbox`` CLI through :mod:`commands`.

Named ``handlers`` rather than ``tools`` so the module can never shadow Hermes' own ``tools`` package
when the repository root is on ``sys.path`` (tests, editable checkouts).
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, Dict

from .client import NetBoxClient, NetBoxError, is_configured, validate_endpoint
from .executor import ExecutionRefused, apply_plan, render_journal, rollback_plan
from .planner import PlanError, build_plan, render_plan
from .settings import get_settings
from .store import get_store

_client_factory: Callable[[], NetBoxClient] = NetBoxClient.from_env


def set_client_factory(factory: Callable[[], NetBoxClient] | None) -> None:
    """Test seam: swap how handlers obtain a client."""
    global _client_factory
    _client_factory = factory or NetBoxClient.from_env


def check_requirements() -> bool:
    """``check_fn`` for registration: tools are exposed only when NETBOX_URL and NETBOX_TOKEN are set."""
    return is_configured()


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
        return _ok(status=client.status())
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
def netbox_plan(args: Dict[str, Any], **_: Any) -> str:
    client = _client_factory()
    settings = get_settings()
    plan = build_plan(client, args.get("operations"), str(args.get("description") or ""), settings)
    get_store().save(plan)
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
def netbox_apply(args: Dict[str, Any], **_: Any) -> str:
    plan_id = str(args.get("plan_id") or "").strip()
    store = get_store()
    plan = store.load(plan_id)
    if plan is None:
        return _fail(f"unknown plan_id {plan_id!r}")
    client = _client_factory()
    dry_run = _bool(args.get("dry_run"), False)
    rollback = _bool(args.get("rollback_on_failure"), True)
    plan = apply_plan(client, plan, store, get_settings(), rollback_on_failure=rollback, dry_run=dry_run)
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
            "rollback": plan.get("rollback"),
            "journal": plan["journal"],
            "report": render_journal(plan),
        }
    )


@guarded
def netbox_rollback(args: Dict[str, Any], **_: Any) -> str:
    plan_id = str(args.get("plan_id") or "").strip()
    store = get_store()
    plan = store.load(plan_id)
    if plan is None:
        return _fail(f"unknown plan_id {plan_id!r}")
    client = _client_factory()
    plan = rollback_plan(client, plan, store, force=_bool(args.get("force"), False), reason="requested")
    rb = plan.get("rollback", {})
    return _json(
        {
            "success": plan["status"] == "rolled_back",
            **_plan_summary(plan),
            "rollback": rb,
            "journal": plan["journal"],
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
