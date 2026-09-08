"""Human-facing time rendering. Storage is always UTC; reports show the operator's zone.

The zone comes from Hermes' ``timezone`` setting (``hermes_time.get_timezone``) when Hermes is
importable, else the host's local zone. Nothing here is used for comparisons or persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo


def get_zone() -> tzinfo | None:
    """Configured display zone, or ``None`` meaning the host's local zone."""
    try:
        from hermes_time import get_timezone  # type: ignore

        return get_timezone()
    except Exception:
        return None


def parse_utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local(stamp: str | None, zone: tzinfo | None = None) -> str:
    """``2026-09-08 12:35:50 PDT`` for a stored UTC stamp; the input unchanged when it does not parse."""
    parsed = parse_utc(stamp)
    if parsed is None:
        return stamp or ""
    zone = zone if zone is not None else get_zone()
    shown = parsed.astimezone(zone) if zone is not None else parsed.astimezone()
    name = shown.tzname() or ""
    return shown.strftime("%Y-%m-%d %H:%M:%S") + (f" {name}" if name else "")
