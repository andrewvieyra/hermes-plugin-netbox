"""Operator entry points that bypass the model: the ``/netbox`` slash command inside a session and the
``hermes netbox`` CLI subcommand. Both reuse the tool handlers so behaviour is identical everywhere."""

from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List

from . import handlers

_HELP = """\
/netbox — NetBox change management

  status                      Connectivity check and NetBox version
  plans [status]              List saved plans (optionally filtered by status)
  show <plan_id>              Full diff, journal and rollback outcome of one plan
  check <plan_id>             Re-verify a plan's preconditions without writing
  apply <plan_id> [--no-rollback]   Apply a reviewed plan
  rollback <plan_id> [--force]      Revert an applied plan from its journal
"""


def _parse(raw: str) -> List[str]:
    try:
        return shlex.split(raw or "")
    except ValueError:
        return (raw or "").split()


def _result(text: str, key: str | None = None) -> str:
    """Render a handler's JSON for a human: the ``report``/``diff`` text when present, else pretty JSON."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not data.get("success", True) and data.get("error"):
        lines = [f"Error: {data['error']}"]
        for err in data.get("errors") or []:
            lines.append(f"  [{err.get('index')}] {err.get('error')}")
        return "\n".join(lines)
    if key and data.get(key):
        return data[key]
    for candidate in ("report", "diff"):
        if data.get(candidate):
            return data[candidate]
    return json.dumps(data, indent=2, ensure_ascii=False)


def run(argv: List[str]) -> str:
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return _result(handlers.netbox_query({"endpoint": "status"}))
    if cmd == "plans":
        args: Dict[str, Any] = {"limit": 50}
        if rest:
            args["status"] = rest[0]
        data = json.loads(handlers.netbox_plans(args))
        if not data.get("success"):
            return f"Error: {data.get('error')}"
        if not data["plans"]:
            return "No plans saved."
        lines = []
        for p in data["plans"]:
            s = p.get("summary", {})
            counts = ", ".join(f"{s.get(a, 0)}{a[0]}" for a in ("create", "update", "delete", "noop"))
            lines.append(f"{p['plan_id']}  {p['status']:<20} {counts:<18} {p.get('description', '')}")
        return "\n".join(lines)
    if cmd in {"show", "check", "apply", "rollback"}:
        if not rest:
            return f"Usage: /netbox {cmd} <plan_id>"
        plan_id = rest[0]
        flags = set(rest[1:])
        if cmd == "show":
            data = json.loads(handlers.netbox_plans({"plan_id": plan_id}))
            if not data.get("success"):
                return f"Error: {data.get('error')}"
            out = data["diff"]
            if data.get("journal"):
                out += "\n\n" + data["report"]
            return out
        if cmd == "check":
            data = json.loads(handlers.netbox_apply({"plan_id": plan_id, "dry_run": True}))
            if not data.get("success"):
                return f"Error: {data.get('error')}"
            if data["applicable"]:
                return f"{plan_id} is applicable: all preconditions hold."
            return f"{plan_id} is NOT applicable:\n" + "\n".join(
                f"  [{c['index']}] {c['error']}" for c in data["conflicts"]
            )
        if cmd == "apply":
            return _result(
                handlers.netbox_apply({"plan_id": plan_id, "rollback_on_failure": "--no-rollback" not in flags})
            )
        return _result(handlers.netbox_rollback({"plan_id": plan_id, "force": "--force" in flags}))
    return f"Unknown subcommand: {cmd}\n\n{_HELP}"


def slash_handler(raw_args: str) -> str:
    return run(_parse(raw_args))


# -- argparse wiring for ``hermes netbox ...`` ----------------------------------------------------


def setup_cli(parser: Any) -> None:
    sub = parser.add_subparsers(dest="netbox_cmd")
    sub.add_parser("status", help="Connectivity check")
    p = sub.add_parser("plans", help="List saved plans")
    p.add_argument("status", nargs="?", help="Filter by status")
    for name, help_text in (("show", "Show one plan"), ("check", "Re-verify preconditions")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("plan_id")
    p = sub.add_parser("apply", help="Apply a reviewed plan")
    p.add_argument("plan_id")
    p.add_argument("--no-rollback", action="store_true", help="Do not revert completed steps on failure")
    p = sub.add_parser("rollback", help="Revert an applied plan")
    p.add_argument("plan_id")
    p.add_argument("--force", action="store_true", help="Override post-apply modification checks")


def cli_handler(args: Any) -> None:
    argv: List[str] = [getattr(args, "netbox_cmd", None) or "help"]
    for attr in ("status", "plan_id"):
        value = getattr(args, attr, None)
        if isinstance(value, str) and value:
            argv.append(value)
    if getattr(args, "no_rollback", False):
        argv.append("--no-rollback")
    if getattr(args, "force", False):
        argv.append("--force")
    print(run(argv))
