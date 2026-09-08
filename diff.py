"""Field-level comparison between a desired payload and NetBox's API representation.

NetBox returns related objects as nested dicts (``{"id": 3, "name": "NYC", "slug": "nyc", ...}``),
choice fields as ``{"value": "active", "label": "Active"}``, tags/M2M as lists of nested dicts, and
``custom_fields`` as a plain dict. Callers express the desired state in *writable* form: integer
IDs (or ``{"slug": ...}`` / ``{"name": ...}`` lookup dicts) for related objects, bare choice values,
lists for M2M. This module decides equality across those two shapes and converts NetBox's shape back
into a writable payload for rollback.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

# Keys NetBox serialises but never accepts on write. ``*_count`` keys are stripped by suffix.
READ_ONLY_KEYS = frozenset(
    {
        "id",
        "url",
        "display",
        "display_url",
        "created",
        "last_updated",
        "config_context",
        "_depth",
        "_occupied",
        "occupied",
        "link_peers",
        "link_peers_type",
        "connected_endpoints",
        "connected_endpoints_type",
        "connected_endpoints_reachable",
        "cable_end",
        "family",
        "notes_url",
    }
)

# Dict-valued fields that are merged (partial update) on PATCH rather than replaced wholesale.
MERGED_DICT_FIELDS = frozenset({"custom_fields"})

_SCALARS = (int, float, str)


def _is_nested_object(value: Any) -> bool:
    return isinstance(value, dict) and "id" in value and not ("value" in value and "label" in value)


def _is_choice(value: Any) -> bool:
    return isinstance(value, dict) and "value" in value and "label" in value


def _scalar_equal(desired: Any, current: Any) -> bool:
    if desired == current:
        return True
    if isinstance(desired, bool) or isinstance(current, bool):
        return False
    if desired is None or current is None:
        return False
    if isinstance(desired, _SCALARS) and isinstance(current, _SCALARS):
        return str(desired) == str(current)
    return False


def values_equal(desired: Any, current: Any) -> bool:
    """True when *desired* (writable form) already describes *current* (NetBox API form)."""
    if _is_nested_object(current):
        if isinstance(desired, bool):
            return False
        if isinstance(desired, int):
            return desired == current.get("id")
        if isinstance(desired, str):
            return desired in {current.get("slug"), current.get("name"), current.get("display")} or _scalar_equal(
                desired, current.get("id")
            )
        if isinstance(desired, dict):
            return bool(desired) and all(values_equal(v, current.get(k)) for k, v in desired.items())
        return False
    if _is_choice(current):
        if isinstance(desired, dict):
            return _scalar_equal(desired.get("value"), current.get("value"))
        return _scalar_equal(desired, current.get("value"))
    if isinstance(current, list):
        if not isinstance(desired, list):
            return False
        if len(desired) != len(current):
            return False
        remaining = list(current)
        for want in desired:
            for i, have in enumerate(remaining):
                if values_equal(want, have):
                    remaining.pop(i)
                    break
            else:
                return False
        return True
    if isinstance(current, dict):
        if not isinstance(desired, dict):
            return False
        return all(k in current and values_equal(v, current[k]) for k, v in desired.items()) and set(desired) == set(
            current
        )
    return _scalar_equal(desired, current)


def to_writable(value: Any) -> Any:
    """Convert NetBox API form into something PATCH/POST accepts (nested object -> id, choice -> value)."""
    if _is_nested_object(value):
        return value["id"]
    if _is_choice(value):
        return value["value"]
    if isinstance(value, list):
        return [to_writable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_writable(v) for k, v in value.items()}
    return value


def compute_changes(desired: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{field: {"from": <writable old>, "to": <desired new>}}`` for every field that differs.

    ``custom_fields`` compares key-by-key so a partial dict is a partial update; the recorded ``from``
    holds only the keys being touched so rollback restores exactly those.
    """
    changes: Dict[str, Dict[str, Any]] = {}
    for field, want in desired.items():
        have = current.get(field)
        if field in MERGED_DICT_FIELDS and isinstance(want, dict) and isinstance(have, dict):
            touched = {k: v for k, v in want.items() if not (k in have and values_equal(v, have[k]))}
            if touched:
                changes[field] = {"from": {k: to_writable(have.get(k)) for k in touched}, "to": touched}
            continue
        if field not in current or not values_equal(want, have):
            changes[field] = {"from": to_writable(have) if field in current else None, "to": want}
    return changes


def snapshot_to_payload(snapshot: Dict[str, Any], drop: Iterable[str] = ()) -> Dict[str, Any]:
    """Writable POST body that re-creates *snapshot* (read-only keys removed, nested objects -> ids)."""
    skip = set(READ_ONLY_KEYS) | set(drop)
    payload: Dict[str, Any] = {}
    for key, value in snapshot.items():
        if key in skip or key.endswith("_count"):
            continue
        payload[key] = to_writable(value)
    return payload


def format_value(value: Any, limit: int = 60) -> str:
    """Compact single-line rendering for diff output."""
    if value is None:
        return "null"
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        import json

        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_changes(changes: Dict[str, Dict[str, Any]]) -> List[str]:
    return [
        f"{field}: {format_value(c.get('from'))} -> {format_value(c.get('to'))}" for field, c in sorted(changes.items())
    ]


def object_label(obj: Dict[str, Any], fallback: str = "") -> str:
    for key in ("display", "name", "address", "prefix", "label", "slug", "model"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback or (f"#{obj['id']}" if obj.get("id") is not None else "?")


def split_pairs(items: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    return {k: v for k, v in items}
