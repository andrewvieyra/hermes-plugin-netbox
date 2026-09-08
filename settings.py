"""Operator-tunable settings, read from ``plugins.entries.netbox.settings`` in config.yaml.

Defaults are conservative: deletes are off, plans are capped, and stale plans are refused.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

WRITE_MODES = ("full", "operator_only", "read_only")


@dataclass
class Settings:
    allow_delete: bool = False
    max_operations: int = 200
    max_query_results: int = 500
    max_plan_age_hours: int = 24
    audit_log: bool = True
    audit_log_path: str = ""
    audit_include_request: bool = True
    write_mode: str = "full"  # full | operator_only | read_only
    plan_retention_days: int = 90  # 0 keeps plans forever
    link_changelog: bool = True  # cross-reference NetBox's object-changes after apply/rollback
    audit_sinks: List[Dict[str, Any]] = field(default_factory=list)  # see sinks.py

    @classmethod
    def from_ctx(cls, ctx: Any) -> Settings:
        """Build from a Hermes ``PluginContext``; every read is guarded so registration never fails."""
        base = cls()
        values: Dict[str, Any] = {}
        for name, default in asdict(base).items():
            try:
                raw = ctx.get_config(name, default=default)
            except Exception:
                raw = default
            values[name] = _coerce(raw, default)
        settings = cls(**values)
        if settings.write_mode not in WRITE_MODES:
            settings.write_mode = "full"
        return settings

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _coerce(raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw) if raw is not None else default
    if isinstance(default, int):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default
    if isinstance(default, list):
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else default
    if isinstance(default, str):
        return str(raw).strip().lower() if raw is not None else default
    return raw if raw is not None else default


_current = Settings()


def get_settings() -> Settings:
    return _current


def set_settings(settings: Settings) -> None:
    global _current
    _current = settings
