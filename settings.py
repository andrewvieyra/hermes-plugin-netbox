"""Operator-tunable settings, read from ``plugins.entries.netbox.settings`` in config.yaml.

Defaults are conservative: deletes are off, plans are capped, and stale plans are refused.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass
class Settings:
    allow_delete: bool = False
    max_operations: int = 200
    max_query_results: int = 500
    max_plan_age_hours: int = 24
    audit_log: bool = True
    audit_log_path: str = ""
    audit_include_request: bool = True

    @classmethod
    def from_ctx(cls, ctx: Any) -> Settings:
        """Build from a Hermes ``PluginContext``; every read is guarded so registration never fails."""
        base = cls()
        values: Dict[str, Any] = {}
        for field, default in asdict(base).items():
            try:
                raw = ctx.get_config(field, default=default)
            except Exception:
                raw = default
            values[field] = _coerce(raw, default)
        return cls(**values)

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
        return value if value > 0 else default
    return raw if raw is not None else default


_current = Settings()


def get_settings() -> Settings:
    return _current


def set_settings(settings: Settings) -> None:
    global _current
    _current = settings
