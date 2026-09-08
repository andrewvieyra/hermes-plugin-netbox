"""hermes-plugin-netbox — NetBox change management plugin for Hermes Agent.

Registers five tools (query / plan / apply / rollback / plans), a ``/netbox`` slash command, a
``hermes netbox`` CLI subcommand, and the bundled ``netbox:workflow`` skill. Registration performs no
network I/O; the NetBox client is created lazily inside each tool call.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import commands, handlers, schemas
from .settings import Settings, set_settings
from .version import __version__

__all__ = ["__version__", "register"]

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent
_TOOLSET = "netbox"
_EMOJI = {"netbox_query": "🔎", "netbox_plan": "📋", "netbox_apply": "🚀", "netbox_rollback": "⏪", "netbox_plans": "🗂️"}
_REQUIRES_ENV = ["NETBOX_URL", "NETBOX_TOKEN"]


def register(ctx) -> None:
    """Called once by Hermes' plugin loader."""
    set_settings(Settings.from_ctx(ctx))
    from . import audit

    audit.set_audit_log(None)  # re-resolve path/enabled from the fresh settings

    for schema in schemas.ALL:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset=_TOOLSET,
            schema=schema,
            handler=handlers.HANDLERS[name],
            check_fn=handlers.check_requirements,
            requires_env=_REQUIRES_ENV,
            emoji=_EMOJI.get(name, ""),
            description=schema["description"].split(". ")[0],
        )

    ctx.register_command(
        "netbox",
        handler=commands.slash_handler,
        args_hint="<status|plans|show|check|apply|rollback> [plan_id]",
        description="NetBox change management: list, inspect, apply or roll back plans",
    )

    try:
        ctx.register_cli_command(
            name="netbox",
            help="NetBox plans: status, plans, show, check, apply, rollback",
            setup_fn=commands.setup_cli,
            handler_fn=commands.cli_handler,
            description="Operate on NetBox change plans from the shell (no model involved).",
        )
    except Exception as exc:  # older Hermes without CLI registration keeps working without it
        logger.debug("netbox plugin: CLI command not registered: %s", exc)

    skill = _PLUGIN_DIR / "SKILL.md"
    if skill.exists():
        try:
            ctx.register_skill("workflow", skill, description="Plan, review, apply and roll back NetBox changes safely")
        except Exception as exc:
            logger.debug("netbox plugin: skill not registered: %s", exc)
