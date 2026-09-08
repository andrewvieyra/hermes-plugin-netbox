"""Tool schemas — the contract the model sees. Keep descriptions precise: they are the only
guidance the model gets at call time (the bundled SKILL.md carries the longer workflow)."""

from __future__ import annotations

from typing import Any, Dict

ENDPOINT = {
    "type": "string",
    "description": "NetBox API endpoint as 'app/model', e.g. 'dcim/devices', 'ipam/ip-addresses', "
    "'dcim/interfaces', 'virtualization/virtual-machines'. Plugin endpoints are 'plugins/<app>/<model>'.",
}

OPERATION = {
    "type": "object",
    "description": "One desired change.",
    "properties": {
        "op": {"type": "string", "enum": ["create", "update", "delete"]},
        "endpoint": ENDPOINT,
        "id": {"type": "integer", "description": "Target object id (update/delete). Mutually exclusive with match."},
        "match": {
            "type": "object",
            "description": 'NetBox filter parameters that identify exactly one object, e.g. {"name": "sw1", '
            '"site": "nyc"}. For update/delete this locates the target. For create it makes the '
            "operation an upsert: an existing match is diffed and updated instead of duplicated.",
            "additionalProperties": True,
        },
        "data": {
            "type": "object",
            "description": "Desired field values in writable form: related objects as integer ids or lookup dicts "
            '({"slug": "nyc"}), choice fields as their value (\'active\'), tags as a list, '
            "custom_fields as a partial dict. Only fields listed here are compared and changed.",
            "additionalProperties": True,
        },
    },
    "required": ["op", "endpoint"],
    "additionalProperties": False,
}


def _schema(name: str, description: str, properties: Dict[str, Any], required=None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


NETBOX_QUERY = _schema(
    "netbox_query",
    "Read from NetBox. Lists objects from an endpoint with optional filters, or fetches one object by id. "
    "Read-only. Use endpoint 'status' to check connectivity and the NetBox version. Prefer 'fields' or "
    "'brief' to keep results small; results are paginated automatically up to 'limit'.",
    {
        "endpoint": ENDPOINT,
        "id": {"type": "integer", "description": "Fetch this single object instead of listing."},
        "filters": {
            "type": "object",
            "description": 'Query parameters, e.g. {"site": "nyc", "status": "active", '
            '"name__ic": "core"}. Any NetBox filter or lookup works.',
            "additionalProperties": True,
        },
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Only return these fields (NetBox >= 4.0 'fields' parameter).",
        },
        "brief": {"type": "boolean", "description": "Return NetBox's brief representation (id, name, display, url)."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum objects to return (default 50)."},
    },
    required=["endpoint"],
)

NETBOX_PLAN = _schema(
    "netbox_plan",
    "Build a change plan WITHOUT writing anything. Resolves every target, computes a field-level diff against "
    "the live objects, and saves the plan with a plan_id. Always show the returned diff to the user and get "
    "their confirmation before calling netbox_apply. Operations that cannot be resolved (missing object, "
    "ambiguous match, invalid endpoint) fail the whole plan with per-operation errors — fix and re-plan.",
    {
        "description": {
            "type": "string",
            "description": "One line describing the intent of the change (shown in plan listings).",
        },
        "operations": {
            "type": "array",
            "items": OPERATION,
            "minItems": 1,
            "description": "Ordered list of changes. Order matters: later operations may depend on earlier ones.",
        },
    },
    required=["description", "operations"],
)

NETBOX_APPLY = _schema(
    "netbox_apply",
    "Apply a plan produced by netbox_plan, in order, with a persisted journal. Before each step the object is "
    "re-read and the step is refused if it changed since planning. On any failure the completed steps are "
    "rolled back automatically (unless rollback_on_failure is false). Only call this after the user has "
    "reviewed the plan diff. dry_run=true re-checks preconditions and writes nothing.",
    {
        "plan_id": {"type": "string", "description": "The plan_id returned by netbox_plan."},
        "rollback_on_failure": {
            "type": "boolean",
            "description": "Revert completed steps if a later step fails (default true).",
        },
        "dry_run": {"type": "boolean", "description": "Only verify the plan is still applicable; write nothing."},
    },
    required=["plan_id"],
)

NETBOX_ROLLBACK = _schema(
    "netbox_rollback",
    "Revert an applied (or partially applied) plan using its journal, newest step first: created objects are "
    "deleted, updated fields are restored, deleted objects are re-created from their snapshot (with a new id). "
    "Objects modified by someone else after the apply are skipped as conflicts unless force=true.",
    {
        "plan_id": {"type": "string"},
        "force": {"type": "boolean", "description": "Override post-apply modification checks (default false)."},
    },
    required=["plan_id"],
)

NETBOX_PLANS = _schema(
    "netbox_plans",
    "List saved plans, or show one plan in full (diff, journal, rollback outcome) when plan_id is given.",
    {
        "plan_id": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "planned",
                "applying",
                "applied",
                "failed",
                "rolling_back",
                "rolled_back",
                "partially_rolled_back",
            ],
        },
        "limit": {"type": "integer", "minimum": 1, "description": "Number of plans to list (default 20)."},
    },
)

ALL = (NETBOX_QUERY, NETBOX_PLAN, NETBOX_APPLY, NETBOX_ROLLBACK, NETBOX_PLANS)
