"""Turn a list of desired operations into a reviewed, persisted plan.

Planning is read-only. For every operation the planner fetches the current object (by ``id`` or by
``match`` filters), computes the field-level diff, and records the ``last_updated`` stamp it saw so
that ``executor.apply_plan`` can refuse to run against an object that changed in between.

Operation shapes accepted from the model::

    {"op": "create", "endpoint": "dcim/devices", "data": {...}, "match": {"name": "sw1"}}   # match optional
    {"op": "update", "endpoint": "dcim/devices", "id": 42, "data": {"status": "planned"}}
    {"op": "update", "endpoint": "dcim/devices", "match": {"name": "sw1"}, "data": {...}}
    {"op": "delete", "endpoint": "ipam/ip-addresses", "id": 7}

A ``create`` with ``match`` is an upsert: when exactly one object matches it becomes an ``update``
(or a ``noop`` when nothing differs); when nothing matches it stays a ``create`` whose apply-time
precondition is "still no match".
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .audit import environment
from .client import NetBoxClient, NetBoxError, validate_endpoint
from .diff import compute_changes, format_value, object_label, render_changes
from .settings import Settings
from .store import new_plan_id, now_iso

VALID_OPS = ("create", "update", "delete")
ACTIONS = ("create", "update", "delete", "noop")


class PlanError(Exception):
    """Raised when the operation list cannot be turned into a plan. ``errors`` is per-operation."""

    def __init__(self, errors: List[Dict[str, Any]]):
        super().__init__(f"{len(errors)} invalid operation(s)")
        self.errors = errors


def _err(index: int, message: str, **extra: Any) -> Dict[str, Any]:
    return {"index": index, "error": message, **extra}


def _positive_int(value: Any) -> int | None:
    """``value`` as a positive int (``"7"`` counts, ``True`` does not), else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _validate_operation(index: int, op: Any, settings: Settings) -> Tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
    """Normalise one raw operation; returns ``(clean_op, errors)``."""
    errors: List[Dict[str, Any]] = []
    if not isinstance(op, dict):
        return None, [_err(index, "operation must be an object")]
    kind = str(op.get("op") or "").strip().lower()
    try:
        endpoint = validate_endpoint(op.get("endpoint", ""))
    except NetBoxError as exc:
        endpoint = ""
        errors.append(_err(index, str(exc)))
    if kind not in VALID_OPS:
        errors.append(_err(index, f"op must be one of {', '.join(VALID_OPS)} (got {op.get('op')!r})"))
        return None, errors
    object_id = _positive_int(op.get("id"))
    if op.get("id") is not None and object_id is None:
        errors.append(_err(index, f"id must be a positive integer (got {op.get('id')!r})"))
    match = op.get("match")
    if match is not None and (not isinstance(match, dict) or not match):
        errors.append(_err(index, "match must be a non-empty object of NetBox filter parameters"))
        match = None
    data = op.get("data")
    if kind in {"create", "update"}:
        if not isinstance(data, dict) or not data:
            errors.append(_err(index, f"{kind} requires a non-empty data object"))
            data = {}
    elif data:
        errors.append(_err(index, "delete does not take data"))
    if kind == "create" and object_id is not None:
        errors.append(_err(index, "create does not take id; use match for upsert semantics"))
    if kind in {"update", "delete"} and object_id is None and match is None:
        errors.append(_err(index, f"{kind} requires id or match"))
    if object_id is not None and match is not None:
        errors.append(_err(index, "give id or match, not both"))
    if kind == "delete" and not settings.allow_delete:
        errors.append(
            _err(
                index,
                "delete operations are disabled; set plugins.entries.netbox.settings."
                "allow_delete: true in config.yaml to permit them",
            )
        )
    if errors:
        return None, errors
    return {"op": kind, "endpoint": endpoint, "id": object_id, "match": match, "data": data or {}}, []


def _resolve_target(client: NetBoxClient, op: Dict[str, Any]) -> Tuple[Dict[str, Any] | None, str | None, str]:
    """``(object, error, resolved_by)`` for an operation addressed by id or match."""
    if op["id"] is not None:
        obj = client.get_object(op["endpoint"], op["id"])
        if obj is None:
            return None, f"{op['endpoint']} #{op['id']} does not exist", "id"
        return obj, None, "id"
    count, obj = client.find(op["endpoint"], op["match"])
    if count > 1:
        return None, f"match {format_value(op['match'])} on {op['endpoint']} is ambiguous ({count} objects)", "match"
    return obj, None, "match"


def _step(index: int, action: str, op: Dict[str, Any], obj: Dict[str, Any] | None, **extra: Any) -> Dict[str, Any]:
    step: Dict[str, Any] = {
        "index": index,
        "action": action,
        "endpoint": op["endpoint"],
        "object_id": obj.get("id") if obj else None,
        "label": object_label(obj) if obj else object_label(op["data"], fallback="(new)"),
        "requested_op": op["op"],
        "resolved_by": extra.pop("resolved_by", None),
    }
    if obj is not None:
        stamp = obj.get("last_updated")
        step["precondition"] = {"last_updated": stamp} if stamp else {}
    step.update(extra)
    return step


def _netbox_version(client: NetBoxClient) -> str | None:
    """Recorded for audit; a failure here must not fail planning."""
    try:
        return str(client.status().get("netbox-version") or "") or None
    except Exception:
        return None


def build_plan(
    client: NetBoxClient,
    operations: Any,
    description: str,
    settings: Settings,
    netbox_url: str = "",
    *,
    actor: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Validate, resolve and diff every operation; return a persisted-ready plan dict. Raises PlanError.
    ``actor`` (see :mod:`audit`) is recorded as ``requested_by``."""
    if not isinstance(operations, list) or not operations:
        raise PlanError([_err(-1, "operations must be a non-empty list")])
    if len(operations) > settings.max_operations:
        raise PlanError(
            [_err(-1, f"plan has {len(operations)} operations; max_operations is {settings.max_operations}")]
        )

    errors: List[Dict[str, Any]] = []
    warnings: List[str] = []
    steps: List[Dict[str, Any]] = []
    for index, raw in enumerate(operations):
        op, op_errors = _validate_operation(index, raw, settings)
        if op_errors:
            errors.extend(op_errors)
            continue
        try:
            if op["op"] == "create":
                steps.append(_plan_create(client, index, op, warnings))
            elif op["op"] == "update":
                steps.append(_plan_update(client, index, op))
            else:
                steps.append(_plan_delete(client, index, op))
        except NetBoxError as exc:
            errors.append(_err(index, str(exc), netbox=exc.to_dict()))
        except _StepError as exc:
            errors.append(_err(index, str(exc)))
    if errors:
        raise PlanError(errors)

    summary = {action: sum(1 for s in steps if s["action"] == action) for action in ACTIONS}
    return {
        "id": new_plan_id(),
        "version": 1,
        "created_at": now_iso(),
        "status": "planned",
        "description": (description or "").strip(),
        "netbox_url": netbox_url or client.base_url,
        "summary": summary,
        "warnings": warnings,
        "steps": steps,
        "journal": [],
        "requested_by": actor,
        "audit": {**environment(), "netbox_version": _netbox_version(client)},
    }


class _StepError(Exception):
    pass


def _plan_create(client: NetBoxClient, index: int, op: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    if op["match"]:
        missing = [k for k in op["match"] if k not in op["data"] and not k.endswith("_id")]
        if missing:
            warnings.append(
                f"[{index}] match keys {missing} are not in data; the created object may not match its own lookup"
            )
        obj, error, _ = _resolve_target(client, op)
        if error:
            raise _StepError(error)
        if obj is not None:
            changes = compute_changes(op["data"], obj)
            if not changes:
                return _step(index, "noop", op, obj, resolved_by="match", reason="already matches desired state")
            return _step(
                index,
                "update",
                op,
                obj,
                resolved_by="match",
                changes=changes,
                payload={k: v["to"] for k, v in changes.items()},
                upsert=True,
            )
        return _step(index, "create", op, None, resolved_by="match", data=op["data"], exists_check=op["match"])
    return _step(index, "create", op, None, data=op["data"])


def _plan_update(client: NetBoxClient, index: int, op: Dict[str, Any]) -> Dict[str, Any]:
    obj, error, how = _resolve_target(client, op)
    if error:
        raise _StepError(error)
    if obj is None:
        raise _StepError(f"no {op['endpoint']} object matches {format_value(op['match'])}")
    changes = compute_changes(op["data"], obj)
    if not changes:
        return _step(index, "noop", op, obj, resolved_by=how, reason="already matches desired state")
    return _step(
        index, "update", op, obj, resolved_by=how, changes=changes, payload={k: v["to"] for k, v in changes.items()}
    )


def _plan_delete(client: NetBoxClient, index: int, op: Dict[str, Any]) -> Dict[str, Any]:
    obj, error, how = _resolve_target(client, op)
    if error:
        raise _StepError(error)
    if obj is None:
        raise _StepError(f"no {op['endpoint']} object matches {format_value(op['match'])}")
    return _step(index, "delete", op, obj, resolved_by=how, snapshot=obj)


# -- rendering ---------------------------------------------------------------------------------


def render_step(step: Dict[str, Any]) -> List[str]:
    parts = [f"[{step['index']}]", f"{step['action']:<6}", step["endpoint"]]
    if step.get("object_id") is not None:
        parts.append(f"#{step['object_id']}")
    parts.append(f'"{step.get("label", "")}"')
    lines = [" ".join(parts)]
    action = step["action"]
    if action == "update":
        lines += [f"      {line}" for line in render_changes(step.get("changes", {}))]
    elif action == "create":
        data = step.get("data", {})
        lines.append("      " + ", ".join(f"{k}={format_value(v, 40)}" for k, v in data.items()))
        if step.get("exists_check"):
            lines.append(f"      precondition: no {step['endpoint']} matches {format_value(step['exists_check'])}")
    elif action == "delete":
        lines.append("      object will be removed; a snapshot is kept for rollback (re-created with a new id)")
    elif action == "noop":
        lines.append(f"      {step.get('reason', 'no change')}")
    return lines


def render_plan(plan: Dict[str, Any]) -> str:
    lines = [
        f"Plan {plan['id']} ({plan['status']})" + (f" — {plan['description']}" if plan.get("description") else ""),
        f"NetBox: {plan.get('netbox_url', '')}",
    ]
    for step in plan["steps"]:
        lines += render_step(step)
    s = plan.get("summary", {})
    lines.append("Summary: " + ", ".join(f"{s.get(a, 0)} {a}" for a in ACTIONS))
    for warning in plan.get("warnings", []):
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)
