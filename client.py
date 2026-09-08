"""Thin NetBox REST client used by the planner and executor.

Deliberately small: token auth (v1 ``Token`` or v2 ``Bearer nbt_``), JSON in/out, offset
pagination, and typed errors. No object-model knowledge lives here; endpoints are passed as
``app/model`` paths relative to ``/api/`` (``dcim/devices``, ``ipam/ip-addresses``).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

_ENDPOINT_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*){1,2}$")
_DEFAULT_TIMEOUT = 30.0


class NetBoxError(Exception):
    """Any failed NetBox request. ``status`` is the HTTP status (0 for transport errors)."""

    def __init__(self, message: str, status: int = 0, body: Any = None, method: str = "", path: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.method = method
        self.path = path

    def field_errors(self) -> Dict[str, Any]:
        """DRF-style ``{field: [messages]}`` from a 400 body, else ``{}``."""
        if isinstance(self.body, dict):
            return {k: v for k, v in self.body.items() if k not in {"detail", "non_field_errors"}}
        return {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self),
            "status": self.status,
            "method": self.method,
            "path": self.path,
            "body": self.body if isinstance(self.body, (dict, list)) else (str(self.body)[:500] if self.body else None),
        }


class ConfigError(NetBoxError):
    """Missing or invalid NETBOX_URL / NETBOX_TOKEN."""


def validate_endpoint(endpoint: str) -> str:
    """Normalise ``/api/dcim/devices/`` or ``dcim/devices`` to ``dcim/devices``; raise on junk."""
    if not isinstance(endpoint, str):
        raise NetBoxError(f"endpoint must be a string like 'dcim/devices' (got {type(endpoint).__name__})")
    clean = endpoint.strip().strip("/")
    if clean.startswith("api/"):
        clean = clean[4:]
    if not _ENDPOINT_RE.match(clean):
        raise NetBoxError(f"invalid endpoint {endpoint!r}; expected 'app/model' such as 'dcim/devices'")
    return clean


def auth_header(token: str) -> str:
    """NetBox v2 tokens (``nbt_<key>.<secret>``) use Bearer; legacy tokens use ``Token``."""
    token = token.strip()
    if token.lower().startswith("bearer ") or token.lower().startswith("token "):
        return token
    return f"Bearer {token}" if token.startswith("nbt_") else f"Token {token}"


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def settings_from_env(env: Dict[str, str] | None = None) -> Dict[str, Any]:
    """Resolve connection settings from the environment; raises ConfigError when incomplete."""
    env = os.environ if env is None else env
    url = (env.get("NETBOX_URL") or "").strip().rstrip("/")
    token = (env.get("NETBOX_TOKEN") or "").strip()
    if not url or not token:
        raise ConfigError("NETBOX_URL and NETBOX_TOKEN must both be set (put them in ~/.hermes/.env)")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"NETBOX_URL must start with http:// or https:// (got {url!r})")
    try:
        timeout = float(env.get("NETBOX_TIMEOUT") or _DEFAULT_TIMEOUT)
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    return {"url": url, "token": token, "verify_ssl": _truthy(env.get("NETBOX_VERIFY_SSL"), True), "timeout": timeout}


def is_configured(env: Dict[str, str] | None = None) -> bool:
    try:
        settings_from_env(env)
        return True
    except NetBoxError:
        return False


class NetBoxClient:
    """``session`` is anything with ``request(method, url, params=, json=, timeout=, verify=)`` returning an
    object with ``status_code``, ``text`` and ``json()`` — ``requests.Session`` in production, a fake in tests."""

    def __init__(
        self, url: str, token: str, *, verify_ssl: bool = True, timeout: float = _DEFAULT_TIMEOUT, session: Any = None
    ):
        self.base_url = url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._headers = {
            "Authorization": auth_header(token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if session is None:
            import requests  # imported lazily so plugin registration never needs network libs

            session = requests.Session()
        self._session = session
        self.last_call: Dict[str, Any] = {}

    @classmethod
    def from_env(cls, env: Dict[str, str] | None = None, session: Any = None) -> NetBoxClient:
        s = settings_from_env(env)
        return cls(s["url"], s["token"], verify_ssl=s["verify_ssl"], timeout=s["timeout"], session=session)

    # -- low level ---------------------------------------------------------------------------
    def _url(self, path: str) -> str:
        path = path.strip("/")
        return f"{self.base_url}/api/{path}/" if path else f"{self.base_url}/api/"

    def request(self, method: str, path: str, *, params: Dict[str, Any] | None = None, json: Any = None) -> Any:
        url = self._url(path)
        try:
            resp = self._session.request(
                method,
                url,
                params=params or None,
                json=json,
                headers=self._headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except Exception as exc:  # transport failure — DNS, TLS, refused, timeout
            self.last_call = {"method": method, "path": f"/api/{path}/", "status": 0}
            raise NetBoxError(f"{method} {url} failed: {exc}", method=method, path=path) from exc
        status = getattr(resp, "status_code", 0)
        self.last_call = {"method": method, "path": f"/api/{path}/", "status": status}
        body: Any = None
        text = getattr(resp, "text", "") or ""
        if text:
            try:
                body = resp.json()
            except Exception:
                body = text
        if status >= 400:
            detail = body.get("detail") if isinstance(body, dict) and "detail" in body else body
            summary = detail if isinstance(detail, str) else (str(detail)[:400] if detail else "")
            raise NetBoxError(
                f"{method} /api/{path}/ -> HTTP {status}: {summary}", status=status, body=body, method=method, path=path
            )
        return body

    # -- convenience --------------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        body = self.request("GET", "status")
        return body if isinstance(body, dict) else {}

    def get_object(self, endpoint: str, object_id: int) -> Dict[str, Any] | None:
        """Detail GET; ``None`` on 404."""
        try:
            body = self.request("GET", f"{validate_endpoint(endpoint)}/{int(object_id)}")
        except NetBoxError as exc:
            if exc.status == 404:
                return None
            raise
        return body if isinstance(body, dict) else None

    def list(
        self, endpoint: str, params: Dict[str, Any] | None = None, *, max_results: int = 500, page_size: int = 100
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Offset-paginate a list endpoint. Returns ``(total_count, results[:max_results])``."""
        endpoint = validate_endpoint(endpoint)
        query = dict(params or {})
        query["limit"] = min(page_size, max_results) if max_results > 0 else page_size
        query.setdefault("offset", 0)
        results: List[Dict[str, Any]] = []
        total = 0
        while True:
            body = self.request("GET", endpoint, params=query)
            if not isinstance(body, dict) or "results" not in body:
                raise NetBoxError(f"GET /api/{endpoint}/ did not return a paginated list", path=endpoint)
            total = int(body.get("count") or 0)
            results.extend(body.get("results") or [])
            if len(results) >= max_results or not body.get("next") or not body.get("results"):
                break
            query["offset"] = int(query["offset"]) + len(body["results"])
        return total, results[:max_results]

    def find(self, endpoint: str, match: Dict[str, Any]) -> Tuple[int, Dict[str, Any] | None]:
        """Look an object up by filter params. ``(count, object)``; object is set only when count == 1."""
        if not match:
            raise NetBoxError("match filters must not be empty")
        total, results = self.list(endpoint, match, max_results=2)
        return total, (results[0] if total == 1 and results else None)

    CHANGELOG_ENDPOINTS = ("core/object-changes", "extras/object-changes")  # NetBox 4.x, then 3.x

    def object_changes(self, since: str, until: str, *, max_results: int = 1000) -> List[Dict[str, Any]]:
        """NetBox change-log records with ``time`` between *since* and *until* (inclusive, ISO UTC).
        Raises NetBoxError when neither endpoint exists or the token may not read the log (403)."""
        params = {"time_after": since, "time_before": until, "ordering": "time"}
        not_found: NetBoxError | None = None
        for endpoint in self.CHANGELOG_ENDPOINTS:
            try:
                _, rows = self.list(endpoint, params, max_results=max_results, page_size=200)
                return rows
            except NetBoxError as exc:
                if exc.status == 404:
                    not_found = exc
                    continue
                raise
        raise not_found or NetBoxError("object-changes endpoint not found")

    def create(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        body = self.request("POST", validate_endpoint(endpoint), json=data)
        if not isinstance(body, dict) or "id" not in body:
            raise NetBoxError(f"POST /api/{endpoint}/ returned no object id", body=body, path=endpoint)
        return body

    def update(self, endpoint: str, object_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        body = self.request("PATCH", f"{validate_endpoint(endpoint)}/{int(object_id)}", json=data)
        return body if isinstance(body, dict) else {}

    def delete(self, endpoint: str, object_id: int) -> None:
        self.request("DELETE", f"{validate_endpoint(endpoint)}/{int(object_id)}")
