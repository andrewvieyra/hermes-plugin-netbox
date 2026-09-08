import json
import unittest

from .base import PluginTestCase
from .helpers import submodule


class Query(PluginTestCase):
    def call(self, name, **args):
        return json.loads(self.handlers.HANDLERS[name](args))

    def test_status(self):
        out = self.call("netbox_query", endpoint="status")
        self.assertTrue(out["success"])
        self.assertEqual(out["status"]["netbox-version"], "4.3.0")

    def test_list_with_filters_fields_and_limit(self):
        out = self.call("netbox_query", endpoint="dcim/devices", filters={"site": "nyc"}, fields=["name"], limit=1)
        self.assertEqual((out["count"], out["returned"], out["truncated"]), (2, 1, True))
        self.assertEqual(out["results"][0], {"id": 10, "name": "sw1"})
        sent = [c for c in self.nb.calls if c["method"] == "GET"][-1]["params"]
        self.assertEqual(sent["fields"], "name")

    def test_limit_capped_by_settings(self):
        self.settings_mod.set_settings(self.settings_mod.Settings(max_query_results=2))
        out = self.call("netbox_query", endpoint="dcim/devices", limit=500)
        self.assertEqual(out["returned"], 2)

    def test_detail_and_404(self):
        self.assertEqual(self.call("netbox_query", endpoint="dcim/devices", id=10)["object"]["name"], "sw1")
        out = self.call("netbox_query", endpoint="dcim/devices", id=999)
        self.assertFalse(out["success"])
        self.assertEqual(out["status"], 404)

    def test_bad_endpoint_is_structured_error(self):
        out = self.call("netbox_query", endpoint="../etc")
        self.assertFalse(out["success"])
        self.assertIn("invalid endpoint", out["error"])

    def test_unconfigured_client_reports_config_error(self):
        client_mod = submodule("client")
        self.handlers.set_client_factory(lambda: client_mod.NetBoxClient.from_env({}))
        out = self.call("netbox_query", endpoint="status")
        self.assertFalse(out["success"])
        self.assertIn("NETBOX_URL and NETBOX_TOKEN", out["error"])


class PlanApplyRollback(PluginTestCase):
    def call(self, name, **args):
        return json.loads(self.handlers.HANDLERS[name](args))

    def test_end_to_end_through_handlers(self):
        planned = self.call(
            "netbox_plan",
            description="rename",
            operations=[{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "SER-1"}}],
        )
        self.assertTrue(planned["success"])
        self.assertIn("serial:  -> SER-1", planned["diff"])
        self.assertIn("confirm", planned["next_step"])
        pid = planned["plan_id"]

        listed = self.call("netbox_plans")
        self.assertEqual(listed["plans"][0]["plan_id"], pid)
        shown = self.call("netbox_plans", plan_id=pid)
        self.assertEqual(shown["status"], "planned")

        dry = self.call("netbox_apply", plan_id=pid, dry_run=True)
        self.assertTrue(dry["applicable"])
        applied = self.call("netbox_apply", plan_id=pid)
        self.assertTrue(applied["success"])
        self.assertEqual(applied["outcome"], "applied")
        self.assertEqual(self.nb.get("dcim/devices", 10)["serial"], "SER-1")

        again = self.call("netbox_apply", plan_id=pid)
        self.assertFalse(again["success"])
        self.assertTrue(again["refused"])

        rolled = self.call("netbox_rollback", plan_id=pid)
        self.assertTrue(rolled["success"])
        self.assertEqual(self.nb.get("dcim/devices", 10)["serial"], "")
        self.assertIn("Rollback: 1 reverted", rolled["report"])

    def test_plan_errors_are_returned_not_raised(self):
        out = self.call("netbox_plan", description="bad", operations=[{"op": "update", "endpoint": "dcim/devices"}])
        self.assertFalse(out["success"])
        self.assertEqual(out["errors"][0]["index"], 0)
        self.assertEqual(self.call("netbox_plans")["count"], 0)

    def test_noop_plan_says_nothing_to_apply(self):
        out = self.call(
            "netbox_plan",
            description="same",
            operations=[{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"name": "sw1"}}],
        )
        self.assertIn("Nothing to apply", out["next_step"])

    def test_string_booleans_are_coerced(self):
        planned = self.call(
            "netbox_plan",
            description="x",
            operations=[{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}],
        )
        dry = self.call("netbox_apply", plan_id=planned["plan_id"], dry_run="true")
        self.assertTrue(dry.get("dry_run"))
        self.assertEqual(len(self.nb.writes()), 0)
        applied = self.call("netbox_apply", plan_id=planned["plan_id"], dry_run="false", rollback_on_failure="no")
        self.assertEqual(applied["outcome"], "applied")
        self.assertFalse(self.store.load(planned["plan_id"])["apply"]["rollback_on_failure"])
        self.call("netbox_query", endpoint="dcim/devices", brief="yes", limit=1)
        self.assertEqual([c for c in self.nb.calls if c["method"] == "GET"][-1]["params"]["brief"], "1")

    def test_unknown_plan_ids(self):
        for name in ("netbox_apply", "netbox_rollback", "netbox_plans"):
            out = self.call(name, plan_id="nbp-nope")
            self.assertFalse(out["success"])
            self.assertIn("unknown plan_id", out["error"])

    def test_handlers_never_raise(self):
        self.handlers.set_client_factory(lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
        out = self.call("netbox_query", endpoint="status")
        self.assertEqual(out, {"success": False, "error": "RuntimeError: kaboom"})


class Commands(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.commands = submodule("commands")

    def test_slash_flow(self):
        self.assertIn("/netbox — NetBox change management", self.commands.slash_handler(""))
        self.assertEqual(self.commands.slash_handler("plans"), "No plans saved.")
        plan = self.plan(
            [{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}], "slash demo"
        )
        listing = self.commands.slash_handler("plans")
        self.assertIn(plan["id"], listing)
        self.assertIn("slash demo", listing)
        self.assertIn("serial:", self.commands.slash_handler(f"show {plan['id']}"))
        self.assertIn("applicable: all preconditions hold", self.commands.slash_handler(f"check {plan['id']}"))
        self.assertIn("-> done", self.commands.slash_handler(f"apply {plan['id']}"))
        self.assertIn("Rollback: 1 reverted", self.commands.slash_handler(f"rollback {plan['id']}"))
        self.assertIn("No plan matches", self.commands.slash_handler("show nbp-x"))
        short = plan["id"].rsplit("-", 1)[1]
        self.assertIn(plan["id"], self.commands.slash_handler(f"show {short}"))
        self.assertIn("Unknown subcommand", self.commands.slash_handler("frobnicate"))
        self.assertIn("Usage", self.commands.slash_handler("apply"))
        self.assertIn("4.3.0", self.commands.slash_handler("status"))

    def test_cli_wiring(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        parser = argparse.ArgumentParser()
        self.commands.setup_cli(parser)
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.commands.cli_handler(parser.parse_args(["apply", plan["id"], "--no-rollback"]))
            self.commands.cli_handler(parser.parse_args(["rollback", plan["id"], "--force"]))
            self.commands.cli_handler(parser.parse_args(["plans", "rolled_back"]))
        text = buf.getvalue()
        self.assertIn("-> done", text)
        self.assertIn("Rollback: 1 reverted", text)
        self.assertIn("rolled_back", text)


if __name__ == "__main__":
    unittest.main()
