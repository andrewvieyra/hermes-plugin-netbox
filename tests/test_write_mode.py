import json
import unittest

from .base import PluginTestCase
from .helpers import submodule


class WriteMode(PluginTestCase):
    def call(self, name, args, **kwargs):
        return json.loads(self.handlers.HANDLERS[name](args, **kwargs))

    def mode(self, value):
        self.settings_mod.set_settings(self.settings_mod.Settings(allow_delete=True, write_mode=value))

    def planned(self):
        return self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])["id"]

    def test_full_lets_the_model_write(self):
        self.mode("full")
        self.assertTrue(self.call("netbox_apply", {"plan_id": self.planned()})["success"])

    def test_operator_only_refuses_model_and_names_the_command(self):
        self.mode("operator_only")
        pid = self.planned()
        out = self.call("netbox_apply", {"plan_id": pid})
        self.assertFalse(out["success"])
        self.assertTrue(out["refused"])
        self.assertEqual(out["write_mode"], "operator_only")
        self.assertIn(f"/netbox apply {pid}", out["error"])
        self.assertIn(f"hermes netbox apply {pid}", out["error"])
        self.assertEqual(self.nb.writes(), [])
        refused = [e for e in self.events() if e["event"] == "apply_refused"]
        self.assertEqual(refused[-1]["write_mode"], "operator_only")
        # dry run is a read and stays allowed
        self.assertTrue(self.call("netbox_apply", {"plan_id": pid, "dry_run": True})["applicable"])

    def test_operator_only_allows_slash_and_cli(self):
        self.mode("operator_only")
        commands = submodule("commands")
        pid = self.planned()
        self.assertIn("-> done", commands.slash_handler(f"apply {pid}"))
        self.assertIn("Rollback: 1 reverted", commands.run(["rollback", pid], via=self.audit.VIA_CLI))

    def test_operator_only_refuses_model_rollback(self):
        self.mode("full")
        pid = self.planned()
        self.call("netbox_apply", {"plan_id": pid})
        self.mode("operator_only")
        out = self.call("netbox_rollback", {"plan_id": pid})
        self.assertFalse(out["success"])
        self.assertIn("/netbox rollback", out["error"])
        self.assertEqual(self.nb.get("dcim/devices", 10)["serial"], "S")

    def test_read_only_refuses_everyone_and_hides_tools(self):
        self.mode("read_only")
        commands = submodule("commands")
        pid = self.planned()
        self.assertIn("read_only", self.call("netbox_apply", {"plan_id": pid})["error"])
        self.assertIn("read_only", commands.slash_handler(f"apply {pid}"))
        self.assertIn("read_only", commands.run(["rollback", pid], via=self.audit.VIA_CLI))
        self.assertEqual(self.nb.writes(), [])
        import os

        saved = {k: os.environ.get(k) for k in ("NETBOX_URL", "NETBOX_TOKEN")}
        os.environ.update(NETBOX_URL="https://netbox.test", NETBOX_TOKEN="nbt_a.b")
        try:
            self.assertFalse(self.handlers.check_write_requirements())
            self.assertTrue(self.handlers.check_requirements())
            self.mode("full")
            self.assertTrue(self.handlers.check_write_requirements())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.mode("read_only")
        # queries and planning keep working
        self.assertTrue(self.call("netbox_query", {"endpoint": "status"})["success"])

    def test_invalid_mode_falls_back_to_full(self):
        class Ctx:
            def get_config(self, key, default=None):
                return {"write_mode": "yolo"}.get(key, default)

        self.assertEqual(self.settings_mod.Settings.from_ctx(Ctx()).write_mode, "full")

    def test_status_reports_mode_and_version(self):
        self.mode("operator_only")
        out = self.call("netbox_query", {"endpoint": "status"})
        self.assertEqual(out["write_mode"], "operator_only")
        self.assertEqual(out["plugin_version"], submodule("version").__version__)


class CompactOutput(PluginTestCase):
    def call(self, name, args, **kwargs):
        return json.loads(self.handlers.HANDLERS[name](args, **kwargs))

    def test_apply_output_has_no_snapshots_but_plans_does(self):
        pid = self.plan([{"op": "delete", "endpoint": "ipam/ip-addresses", "id": 21}])["id"]
        out = self.call("netbox_apply", {"plan_id": pid})
        entry = out["journal"][0]
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["http_status"], 204)
        self.assertNotIn("inverse", entry)
        self.assertNotIn("snapshot", json.dumps(out))
        full = self.call("netbox_plans", {"plan_id": pid})
        self.assertIn("snapshot", json.dumps(full["journal"][0]["inverse"]))
        rolled = self.call("netbox_rollback", {"plan_id": pid})
        self.assertEqual(rolled["journal"][0]["revert_status"], "reverted")
        self.assertIn("new_object_id", rolled["journal"][0])
        self.assertEqual(
            set(rolled["rollback"]),
            {"started_at", "finished_at", "reason", "force", "entries", "reverted", "conflict", "failed", "changelog"},
        )


class Prune(PluginTestCase):
    def test_prune_by_age_keeps_in_flight(self):
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat().replace("+00:00", "Z")
        for status in ("applied", "rolled_back", "planned", "applying", "failed"):
            plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": status}}])
            plan["status"] = status
            self.store.save(plan)
            path = self.store.directory / f"{plan['id']}.json"
            doc = json.loads(path.read_text())
            doc["updated_at"] = old
            path.write_text(json.dumps(doc))
        fresh = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 11, "data": {"serial": "new"}}])
        preview = self.store.prune(90, dry_run=True)
        self.assertEqual(len(self.store.list(limit=100)), 6)
        removed = self.store.prune(90)
        self.assertEqual(sorted(r["status"] for r in removed), ["applied", "failed", "planned", "rolled_back"])
        self.assertEqual(len(preview), len(removed))
        left = {p["id"]: p["status"] for p in self.store.list(limit=100)}
        self.assertIn(fresh["id"], left)
        self.assertIn("applying", left.values())
        self.assertEqual(self.store.prune(0), [])

    def test_prune_command_and_audit(self):
        commands = submodule("commands")
        self.assertIn("Nothing older than 90 days", commands.slash_handler("prune"))
        self.assertIn("Usage", commands.slash_handler("prune --days x"))
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        out = commands.slash_handler("prune --days 0")
        self.assertIn("Retention is disabled", out)
        from datetime import datetime, timedelta, timezone

        path = self.store.directory / f"{plan['id']}.json"
        doc = json.loads(path.read_text())
        doc["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(doc))
        self.assertIn("Would remove 1 plan", commands.slash_handler("prune --days 3 --dry-run"))
        self.assertTrue(self.store.load(plan["id"]))
        self.assertIn("Removed 1 plan", commands.run(["prune", "--days", "3"], via=self.audit.VIA_CLI))
        self.assertIsNone(self.store.load(plan["id"]))
        pruned = [e for e in self.events() if e["event"] == "plans_pruned"]
        self.assertEqual(pruned[-1]["plan_ids"], [plan["id"]])
        self.assertEqual(pruned[-1]["actor"]["via"], "cli")

    def test_opportunistic_prune_runs_on_plan(self):
        from datetime import datetime, timedelta, timezone

        self.handlers._last_prune_at = None
        self.settings_mod.set_settings(self.settings_mod.Settings(allow_delete=True, plan_retention_days=1))
        stale = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        path = self.store.directory / f"{stale['id']}.json"
        doc = json.loads(path.read_text())
        doc["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(doc))
        self.handlers.HANDLERS["netbox_plan"](
            {
                "description": "x",
                "operations": [{"op": "update", "endpoint": "dcim/devices", "id": 11, "data": {"serial": "T"}}],
            }
        )
        self.assertIsNone(self.store.load(stale["id"]))
        self.assertEqual(len(self.store.list()), 1)


class Rendering(PluginTestCase):
    def test_plan_and_journal_show_local_time_and_actor(self):
        from zoneinfo import ZoneInfo

        timefmt = submodule("timefmt")
        original = timefmt.get_zone
        timefmt.get_zone = lambda: ZoneInfo("America/Los_Angeles")
        try:
            plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
            plan["requested_by"] = {
                "kind": "model",
                "via": "tool",
                "platform": "signal",
                "user_name": "Andrew",
                "chat_type": "dm",
                "chat_name": "Andrew",
                "cron": False,
            }
            text = self.planner.render_plan(plan)
            self.assertRegex(
                text,
                r"Created: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} P[DS]T by model via tool on signal for Andrew in dm Andrew",
            )
            actor = self.audit.capture_actor({}, via="cli")
            applied = self.executor.apply_plan(self.client, plan, self.store, self.settings, actor=actor)
            report = self.executor.render_journal(applied)
            self.assertRegex(report, r"Applied: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} P[DS]T by operator via cli \(")
            rolled = self.executor.rollback_plan(self.client, applied, self.store, actor=actor)
            self.assertIn("Rolled back: ", self.executor.render_journal(rolled))
            self.assertEqual(timefmt.local("2026-09-08T19:35:50Z"), "2026-09-08 12:35:50 PDT")
            self.assertEqual(timefmt.local("garbage"), "garbage")
            self.assertEqual(timefmt.local(None), "")
        finally:
            timefmt.get_zone = original

    def test_describe_actor_variants(self):
        d = self.audit.describe_actor
        self.assertEqual(d(None), "unknown")
        self.assertEqual(d({"kind": "operator", "via": "cli", "os_user": "hermes"}), "operator via cli (hermes)")
        self.assertEqual(
            d(
                {
                    "kind": "model",
                    "via": "tool",
                    "platform": "telegram",
                    "user_id": "42",
                    "chat_type": "group",
                    "chat_name": "netops",
                    "cron": True,
                }
            ),
            "model via tool on telegram for 42 in group netops [cron]",
        )


class ClaimLock(PluginTestCase):
    def test_concurrent_claim_is_refused_not_duplicated(self):
        import threading

        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        other = self.store_mod.PlanStore(self.store.directory)  # a second process would have its own handle
        self.store.claim_timeout = 0.2
        held = threading.Event()
        release = threading.Event()

        def hold():
            with other.exclusive():
                held.set()
                release.wait(5)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        try:
            with self.assertRaises(self.executor.ExecutionRefused) as ctx:
                self.executor.apply_plan(self.client, plan, self.store, self.settings)
            self.assertIn("locked by another process", str(ctx.exception))
            self.assertEqual(self.nb.writes(), [])
        finally:
            release.set()
            t.join(5)
        # once released, the same plan applies normally
        self.assertEqual(
            self.executor.apply_plan(self.client, self.store.load(plan["id"]), self.store, self.settings)["status"],
            "applied",
        )


if __name__ == "__main__":
    unittest.main()
