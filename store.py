"""Plan persistence: one JSON file per plan under the plugin's data directory.

The plan file is the source of truth during apply: the journal is flushed after every step so an
interrupted apply leaves a file that ``netbox_rollback`` can act on.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

PLUGIN_NAME = "netbox"


def default_plans_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/netbox/plans`` — via Hermes' helper when importable."""
    base: Path | None = None
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        base = plugin_data_dir(PLUGIN_NAME)
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        base = Path(home) / "plugin-data" / PLUGIN_NAME
    return base / "plans"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_plan_id() -> str:
    """``nbp-<ISO 8601 basic UTC timestamp>-<4 hex>``, e.g. ``nbp-20260908T193012Z-4f1a``: filename-safe
    (no colons), sorts chronologically, and the ``Z`` makes the zone explicit."""
    return f"nbp-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(2)}"


class PlanStore:
    def __init__(self, directory: Path | None = None):
        self._dir = Path(directory) if directory else None
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """Process-wide lock callers take to make a read-modify-write of one plan atomic."""
        return self._lock

    @property
    def directory(self) -> Path:
        if self._dir is None:
            self._dir = default_plans_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def _path(self, plan_id: str) -> Path:
        if not plan_id or "/" in plan_id or "\\" in plan_id or ".." in plan_id:
            raise ValueError(f"invalid plan id {plan_id!r}")
        return self.directory / f"{plan_id}.json"

    def create(self, plan: Dict[str, Any], *, attempts: int = 5) -> None:
        """First save of a new plan. The file is created exclusively (``O_EXCL``), so an id that already
        exists on disk can never be overwritten; on a collision the plan gets a fresh id and we retry."""
        with self._lock:
            for _ in range(attempts):
                path = self._path(plan["id"])
                plan["updated_at"] = now_iso()
                payload = json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=False, default=str)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    plan["id"] = new_plan_id()
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                return
            raise RuntimeError(f"could not allocate a unique plan id after {attempts} attempts")

    def save(self, plan: Dict[str, Any]) -> None:
        """Persist an existing plan (atomic replace). Use :meth:`create` for a brand-new plan."""
        with self._lock:
            path = self._path(plan["id"])
            tmp = path.with_suffix(".json.tmp")
            plan["updated_at"] = now_iso()
            tmp.write_text(
                json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=False, default=str), encoding="utf-8"
            )
            os.replace(tmp, path)

    def load(self, plan_id: str) -> Dict[str, Any] | None:
        with self._lock:
            path = self._path(plan_id)
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self, status: str | None = None, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            plans: List[Dict[str, Any]] = []
            for path in self.directory.glob("nbp-*.json"):
                try:
                    plan = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if status and plan.get("status") != status:
                    continue
                plans.append(plan)
        plans.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        return plans[: max(1, limit)]

    def resolve(self, fragment: str) -> Tuple[str | None, List[str]]:
        """Map what a human typed to a plan id. An exact id wins; otherwise an unambiguous suffix or
        substring match (``4f1a`` or ``T193550Z-4f1a``) resolves. Returns ``(plan_id, candidates)``:
        ``plan_id`` is None when nothing or more than one plan matches, and ``candidates`` lists the
        matches so the caller can show them."""
        fragment = (fragment or "").strip()
        if not fragment:
            return None, []
        with self._lock:
            try:
                if self._path(fragment).exists():
                    return fragment, [fragment]
            except ValueError:
                return None, []
            ids = sorted(p.stem for p in self.directory.glob("nbp-*.json"))
        needle = fragment.lower()
        candidates = [i for i in ids if i.lower().endswith("-" + needle) or i.lower().endswith(needle)]
        if not candidates:
            candidates = [i for i in ids if needle in i.lower()]
        return (candidates[0], candidates) if len(candidates) == 1 else (None, candidates)

    def delete(self, plan_id: str) -> bool:
        with self._lock:
            path = self._path(plan_id)
            if path.exists():
                path.unlink()
                return True
            return False


_store: PlanStore | None = None
_store_lock = threading.Lock()


def get_store() -> PlanStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = PlanStore()
        return _store


def set_store(store: PlanStore | None) -> None:
    """Test seam / profile switch: replace the process-wide store."""
    global _store
    with _store_lock:
        _store = store
