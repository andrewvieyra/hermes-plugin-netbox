"""In-memory stand-in for a NetBox instance, speaking the subset of the REST API the plugin uses.

It behaves like NetBox where it matters for these tests: offset pagination with ``count``/``next``/
``results``; equality filters that understand ``<field>_id`` and slug/name lookups on nested objects;
nested-object and choice-field serialisation; ``last_updated`` bumped on every write; DRF-style 400
bodies keyed by field; 404 on unknown ids; and an optional per-request failure hook to simulate
mid-plan errors.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qsl, urlsplit

_CHOICES = {"status": {"active": "Active", "planned": "Planned", "offline": "Offline", "reserved": "Reserved"}}


class _Response:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else json.dumps(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeNetBox:
    """``session``-compatible object: ``request(method, url, params=, json=, ...)``."""

    def __init__(self, base_url: str = "https://netbox.test"):
        self.base_url = base_url
        self.tables: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.fail_on: Callable[[str, str, Any], _Response | None] | None = None
        self.required_fields: Dict[str, List[str]] = {}  # endpoint -> fields that must be present on create
        self.reject_fields: Dict[str, List[str]] = {}  # endpoint -> fields that 400 on create (simulates cascade refs)
        self.version = "4.3.0"
        self._clock = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)

    # -- fixtures -----------------------------------------------------------------------------
    def seed(self, endpoint: str, obj: Dict[str, Any]) -> Dict[str, Any]:
        table = self.tables.setdefault(endpoint, {})
        oid = obj.get("id") or (max(table) + 1 if table else 1)
        obj = dict(obj, id=oid)
        obj.setdefault("last_updated", self._tick())
        obj.setdefault("created", obj["last_updated"])
        obj.setdefault("display", obj.get("name") or obj.get("address") or f"#{oid}")
        obj.setdefault("url", f"{self.base_url}/api/{endpoint}/{oid}/")
        table[oid] = obj
        return obj

    def get(self, endpoint: str, oid: int) -> Dict[str, Any] | None:
        return self.tables.get(endpoint, {}).get(oid)

    def _tick(self) -> str:
        self._clock += timedelta(seconds=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    # -- filtering ----------------------------------------------------------------------------
    @staticmethod
    def _matches(obj: Dict[str, Any], key: str, want: Any) -> bool:
        if key.endswith("_id"):
            field = key[:-3]
            value = obj.get(field)
            return isinstance(value, dict) and str(value.get("id")) == str(want)
        if key.endswith("__ic"):
            value = obj.get(key[:-4])
            return isinstance(value, str) and str(want).lower() in value.lower()
        value = obj.get(key)
        if isinstance(value, dict):
            if "value" in value and "label" in value:
                return str(value["value"]) == str(want)
            return str(want) in {str(value.get("id")), value.get("slug"), value.get("name")}
        if isinstance(value, list):
            return any(FakeNetBox._matches({"x": v}, "x", want) for v in value)
        return str(value) == str(want)

    def _filter(self, endpoint: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = list(self.tables.get(endpoint, {}).values())
        for key, want in params.items():
            if key in {"limit", "offset", "fields", "brief", "ordering"}:
                continue
            rows = [r for r in rows if self._matches(r, key, want)]
        return sorted(rows, key=lambda r: r["id"])

    # -- write helpers -------------------------------------------------------------------------
    def _serialise_in(
        self, endpoint: str, data: Dict[str, Any], existing: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """Turn writable input into API form (int -> nested, choice value -> {value,label})."""
        out: Dict[str, Any] = dict(existing or {})
        for key, value in data.items():
            if key in _CHOICES and isinstance(value, str):
                if value not in _CHOICES[key]:
                    raise _FieldError({key: [f'"{value}" is not a valid choice.']})
                out[key] = {"value": value, "label": _CHOICES[key][value]}
            elif key == "custom_fields" and isinstance(value, dict):
                merged = dict((existing or {}).get("custom_fields") or {})
                merged.update(value)
                out[key] = merged
            elif key == "tags" and isinstance(value, list):
                out[key] = [self._resolve_ref("extras/tags", v) for v in value]
            elif (
                isinstance(value, int)
                and not isinstance(value, bool)
                and key in {"site", "role", "device_type", "tenant", "device", "vrf"}
                or isinstance(value, dict)
                and key in {"site", "role", "device_type", "tenant", "device", "vrf"}
            ):
                out[key] = self._resolve_ref(self._ref_endpoint(key), value)
            else:
                out[key] = value
        return out

    @staticmethod
    def _ref_endpoint(field: str) -> str:
        return {
            "site": "dcim/sites",
            "role": "dcim/device-roles",
            "device_type": "dcim/device-types",
            "tenant": "tenancy/tenants",
            "device": "dcim/devices",
            "vrf": "ipam/vrfs",
        }[field]

    def _resolve_ref(self, endpoint: str, ref: Any) -> Dict[str, Any]:
        table = self.tables.get(endpoint, {})
        if isinstance(ref, dict) and "id" in ref:
            ref = ref["id"]
        if isinstance(ref, int):
            obj = table.get(ref)
            if obj is None:
                raise _FieldError(
                    {endpoint.split("/")[-1]: [f"Related object not found using the provided numeric ID: {ref}"]}
                )
        elif isinstance(ref, dict):
            hits = [o for o in table.values() if all(str(o.get(k)) == str(v) for k, v in ref.items())]
            if len(hits) != 1:
                raise _FieldError(
                    {endpoint.split("/")[-1]: [f"Related object lookup {ref} matched {len(hits)} objects"]}
                )
            obj = hits[0]
        else:
            raise _FieldError({endpoint.split("/")[-1]: [f"Invalid reference {ref!r}"]})
        return {k: obj[k] for k in ("id", "url", "display", "name", "slug") if k in obj}

    # -- request dispatch ----------------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        params: Any = None,
        json: Any = None,
        headers: Any = None,
        timeout: Any = None,
        verify: Any = None,
    ) -> _Response:
        parts = urlsplit(url)
        path = parts.path
        assert path.startswith("/api/"), path
        query = dict(parse_qsl(parts.query))
        if params:
            query.update({k: str(v) for k, v in params.items()})
        endpoint, oid = self._split(path[len("/api/") :].strip("/"))
        self.calls.append({"method": method, "endpoint": endpoint, "id": oid, "params": query, "json": json})
        auth = (headers or {}).get("Authorization", "")
        if not auth.startswith(("Token ", "Bearer ")) or len(auth.split(" ", 1)[1].strip()) == 0:
            return _Response(403, {"detail": "Invalid token"})
        if self.fail_on is not None:
            forced = self.fail_on(method, endpoint, json)
            if forced is not None:
                return forced
        try:
            return self._dispatch(method, endpoint, oid, query, json)
        except _FieldError as exc:
            return _Response(400, exc.body)

    @staticmethod
    def _split(rest: str):
        m = re.match(r"^(.*?)/(\d+)$", rest)
        return (m.group(1), int(m.group(2))) if m else (rest, None)

    def _dispatch(self, method: str, endpoint: str, oid: int | None, query: Dict[str, str], body: Any) -> _Response:
        if endpoint == "status":
            return _Response(200, {"netbox-version": self.version, "django-version": "5.1"})
        table = self.tables.setdefault(endpoint, {})
        if method == "GET" and oid is None:
            rows = self._filter(endpoint, query)
            limit = int(query.get("limit", 50))
            offset = int(query.get("offset", 0))
            page = rows[offset : offset + limit]
            nxt = (
                f"{self.base_url}/api/{endpoint}/?limit={limit}&offset={offset + limit}"
                if offset + limit < len(rows)
                else None
            )
            fields = query.get("fields")
            if fields:
                keep = set(fields.split(",")) | {"id"}
                page = [{k: v for k, v in r.items() if k in keep} for r in page]
            return _Response(200, {"count": len(rows), "next": nxt, "previous": None, "results": page})
        if method == "GET":
            obj = table.get(oid)
            return _Response(200, obj) if obj else _Response(404, {"detail": "Not found."})
        if method == "POST":
            for field in self.required_fields.get(endpoint, []):
                if field not in (body or {}):
                    raise _FieldError({field: ["This field is required."]})
            bad = {
                f: ["Invalid reference (simulated cascade)."]
                for f in self.reject_fields.get(endpoint, [])
                if f in (body or {})
            }
            if bad:
                raise _FieldError(bad)
            data = self._serialise_in(endpoint, body or {})
            data.pop("id", None)
            return _Response(201, self.seed(endpoint, data))
        if method == "PATCH":
            obj = table.get(oid)
            if obj is None:
                return _Response(404, {"detail": "Not found."})
            updated = self._serialise_in(endpoint, body or {}, existing=obj)
            updated["last_updated"] = self._tick()
            table[oid] = updated
            return _Response(200, updated)
        if method == "DELETE":
            if oid not in table:
                return _Response(404, {"detail": "Not found."})
            del table[oid]
            return _Response(204, None)
        return _Response(405, {"detail": "Method not allowed."})

    # -- test helpers ------------------------------------------------------------------------------
    def touch(self, endpoint: str, oid: int) -> None:
        """Simulate somebody else editing an object (bumps last_updated only)."""
        self.tables[endpoint][oid]["last_updated"] = self._tick()

    def writes(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["method"] in {"POST", "PATCH", "DELETE"}]


class _FieldError(Exception):
    def __init__(self, body: Dict[str, Any]):
        super().__init__(str(body))
        self.body = body


def seeded() -> FakeNetBox:
    """A small, realistic dataset: two sites, a role, a device type, three devices, two IPs, tags."""
    nb = FakeNetBox()
    nb.seed("dcim/sites", {"id": 1, "name": "New York", "slug": "nyc"})
    nb.seed("dcim/sites", {"id": 2, "name": "San Francisco", "slug": "sfo"})
    nb.seed("dcim/device-roles", {"id": 1, "name": "Switch", "slug": "switch"})
    nb.seed("dcim/device-types", {"id": 1, "model": "EX4300", "slug": "ex4300", "name": "EX4300"})
    nb.seed("extras/tags", {"id": 1, "name": "core", "slug": "core"})
    nb.seed("extras/tags", {"id": 2, "name": "edge", "slug": "edge"})

    def brief(endpoint: str, oid: int, *keys: str) -> Dict[str, Any]:
        obj = nb.get(endpoint, oid)
        return {k: obj[k] for k in keys if k in obj}

    def site(i: int) -> Dict[str, Any]:
        return brief("dcim/sites", i, "id", "url", "display", "name", "slug")

    def tag(i: int) -> Dict[str, Any]:
        return brief("extras/tags", i, "id", "url", "display", "name", "slug")

    role = brief("dcim/device-roles", 1, "id", "url", "display", "name", "slug")
    dtype = brief("dcim/device-types", 1, "id", "url", "display", "model", "slug")
    for oid, name, s, status in ((10, "sw1", 1, "active"), (11, "sw2", 1, "planned"), (12, "sw3", 2, "active")):
        nb.seed(
            "dcim/devices",
            {
                "id": oid,
                "name": name,
                "site": site(s),
                "role": role,
                "device_type": dtype,
                "status": {"value": status, "label": _CHOICES["status"][status]},
                "tags": [tag(1)] if oid == 10 else [],
                "custom_fields": {"owner": "netops", "tier": 1},
                "primary_ip4": None,
                "serial": "",
                "comments": "",
                "interface_count": 4,
            },
        )
    nb.seed(
        "ipam/ip-addresses",
        {
            "id": 20,
            "address": "10.0.0.1/24",
            "status": {"value": "active", "label": "Active"},
            "dns_name": "sw1.example.net",
            "tags": [],
        },
    )
    nb.seed(
        "ipam/ip-addresses",
        {
            "id": 21,
            "address": "10.0.0.2/24",
            "status": {"value": "reserved", "label": "Reserved"},
            "dns_name": "",
            "tags": [],
        },
    )
    nb.calls.clear()
    return nb
