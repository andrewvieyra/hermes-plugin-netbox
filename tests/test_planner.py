import unittest

from .base import PluginTestCase


class Validation(PluginTestCase):
    def errors_for(self, ops, **settings):
        s = self.settings_mod.Settings(allow_delete=True, **settings)
        with self.assertRaises(self.planner.PlanError) as ctx:
            self.planner.build_plan(self.client, ops, "x", s)
        return ctx.exception.errors

    def test_shape_errors_are_collected_per_operation(self):
        errors = self.errors_for(
            [
                {"op": "rename", "endpoint": "dcim/devices", "id": 10, "data": {"name": "a"}},
                {"op": "update", "endpoint": "devices", "id": 10, "data": {"name": "a"}},
                {"op": "update", "endpoint": "dcim/devices", "data": {"name": "a"}},
                {"op": "update", "endpoint": "dcim/devices", "id": 10, "match": {"name": "sw1"}, "data": {"name": "a"}},
                {"op": "create", "endpoint": "dcim/devices", "id": 3, "data": {"name": "a"}},
                {"op": "update", "endpoint": "dcim/devices", "id": 10},
                {"op": "delete", "endpoint": "dcim/devices", "id": 10, "data": {"x": 1}},
                {"op": "update", "endpoint": "dcim/devices", "id": "ten", "data": {"name": "a"}},
                "not an object",
            ]
        )
        messages = {}
        for e in errors:
            messages[e["index"]] = messages.get(e["index"], "") + " | " + e["error"]
        self.assertIn("op must be one of", messages[0])
        self.assertIn("invalid endpoint", messages[1])
        self.assertIn("requires id or match", messages[2])
        self.assertIn("not both", messages[3])
        self.assertIn("create does not take id", messages[4])
        self.assertIn("non-empty data", messages[5])
        self.assertIn("does not take data", messages[6])
        self.assertIn("positive integer", messages[7])
        self.assertIn("must be an object", messages[8])
        self.assertEqual(len(self.nb.writes()), 0)

    def test_delete_disabled_by_default(self):
        s = self.settings_mod.Settings()
        with self.assertRaises(self.planner.PlanError) as ctx:
            self.planner.build_plan(self.client, [{"op": "delete", "endpoint": "dcim/devices", "id": 10}], "x", s)
        self.assertIn("allow_delete", ctx.exception.errors[0]["error"])

    def test_empty_and_oversized_lists(self):
        self.assertIn("non-empty", self.errors_for([])[0]["error"])
        ops = [{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "x"}}] * 3
        self.assertIn("max_operations", self.errors_for(ops, max_operations=2)[0]["error"])

    def test_resolution_errors(self):
        errors = self.errors_for(
            [
                {"op": "update", "endpoint": "dcim/devices", "id": 999, "data": {"serial": "x"}},
                {"op": "update", "endpoint": "dcim/devices", "match": {"site": "nyc"}, "data": {"serial": "x"}},
                {"op": "update", "endpoint": "dcim/devices", "match": {"name": "ghost"}, "data": {"serial": "x"}},
                {"op": "delete", "endpoint": "dcim/devices", "match": {"name": "ghost"}},
            ]
        )
        messages = [e["error"] for e in errors]
        self.assertIn("does not exist", messages[0])
        self.assertIn("ambiguous (2 objects)", messages[1])
        self.assertIn("no dcim/devices object matches", messages[2])
        self.assertIn("no dcim/devices object matches", messages[3])


class Planning(PluginTestCase):
    def test_update_by_id_records_diff_and_precondition(self):
        plan = self.plan(
            [
                {
                    "op": "update",
                    "endpoint": "dcim/devices",
                    "id": 10,
                    "data": {"status": "planned", "site": 2, "name": "sw1"},
                }
            ],
            "move sw1",
        )
        step = plan["steps"][0]
        self.assertEqual(step["action"], "update")
        self.assertEqual(step["changes"], {"status": {"from": "active", "to": "planned"}, "site": {"from": 1, "to": 2}})
        self.assertEqual(step["payload"], {"status": "planned", "site": 2})
        self.assertEqual(step["precondition"]["last_updated"], self.nb.get("dcim/devices", 10)["last_updated"])
        self.assertEqual(step["label"], "sw1")
        self.assertEqual(plan["summary"], {"create": 0, "update": 1, "delete": 0, "noop": 0})
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["netbox_url"], self.nb.base_url)
        self.assertRegex(plan["id"], r"^nbp-\d{8}T\d{6}Z-[0-9a-f]{4}$")
        self.assertEqual(len(self.nb.writes()), 0)

    def test_update_by_match_and_noop(self):
        plan = self.plan(
            [
                {"op": "update", "endpoint": "dcim/devices", "match": {"name": "sw2"}, "data": {"status": "planned"}},
                {
                    "op": "update",
                    "endpoint": "dcim/devices",
                    "match": {"name": "sw2", "site": "nyc"},
                    "data": {"serial": "ABC"},
                },
            ]
        )
        self.assertEqual([s["action"] for s in plan["steps"]], ["noop", "update"])
        self.assertEqual(plan["steps"][0]["resolved_by"], "match")
        self.assertEqual(plan["steps"][1]["object_id"], 11)

    def test_create_plain_and_upsert_variants(self):
        plan = self.plan(
            [
                {
                    "op": "create",
                    "endpoint": "dcim/devices",
                    "data": {"name": "sw9", "site": 1, "role": 1, "device_type": 1},
                },
                {
                    "op": "create",
                    "endpoint": "dcim/devices",
                    "match": {"name": "sw8"},
                    "data": {"name": "sw8", "site": 1, "role": 1, "device_type": 1},
                },
                {
                    "op": "create",
                    "endpoint": "dcim/devices",
                    "match": {"name": "sw1"},
                    "data": {"name": "sw1", "site": 1, "status": "offline"},
                },
                {
                    "op": "create",
                    "endpoint": "dcim/devices",
                    "match": {"name": "sw3"},
                    "data": {"name": "sw3", "site": 2},
                },
                {"op": "create", "endpoint": "dcim/devices", "match": {"name": "sw7"}, "data": {"site": 1}},
            ]
        )
        actions = [s["action"] for s in plan["steps"]]
        self.assertEqual(actions, ["create", "create", "update", "noop", "create"])
        self.assertNotIn("exists_check", plan["steps"][0])
        self.assertEqual(plan["steps"][1]["exists_check"], {"name": "sw8"})
        self.assertTrue(plan["steps"][2]["upsert"])
        self.assertEqual(plan["steps"][2]["changes"], {"status": {"from": "active", "to": "offline"}})
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("['name']", plan["warnings"][0])

    def test_delete_keeps_snapshot(self):
        plan = self.plan([{"op": "delete", "endpoint": "ipam/ip-addresses", "match": {"address": "10.0.0.1/24"}}])
        step = plan["steps"][0]
        self.assertEqual(step["action"], "delete")
        self.assertEqual(step["snapshot"]["dns_name"], "sw1.example.net")
        self.assertEqual(step["label"], "10.0.0.1/24")

    def test_netbox_error_during_planning_is_reported_not_raised(self):
        from .fake_netbox import _Response

        self.nb.fail_on = lambda m, e, b: _Response(500, {"detail": "boom"}) if e == "ipam/ip-addresses" else None
        with self.assertRaises(self.planner.PlanError) as ctx:
            self.planner.build_plan(
                self.client,
                [
                    {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "x"}},
                    {"op": "update", "endpoint": "ipam/ip-addresses", "id": 20, "data": {"dns_name": "x"}},
                ],
                "x",
                self.settings,
            )
        self.assertEqual([e["index"] for e in ctx.exception.errors], [1])
        self.assertEqual(ctx.exception.errors[0]["netbox"]["status"], 500)

    def test_render_plan_is_readable(self):
        plan = self.plan(
            [
                {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"status": "planned"}},
                {
                    "op": "create",
                    "endpoint": "dcim/devices",
                    "match": {"name": "sw8"},
                    "data": {"name": "sw8", "site": 1},
                },
                {"op": "delete", "endpoint": "ipam/ip-addresses", "id": 21},
                {"op": "update", "endpoint": "dcim/devices", "id": 11, "data": {"status": "planned"}},
            ],
            "demo",
        )
        text = self.planner.render_plan(plan)
        self.assertIn("— demo", text)
        self.assertIn('[0] update dcim/devices #10 "sw1"', text)
        self.assertIn("status: active -> planned", text)
        self.assertIn('[1] create dcim/devices "sw8"', text)
        self.assertIn("precondition: no dcim/devices matches", text)
        self.assertIn('[2] delete ipam/ip-addresses #21 "10.0.0.2/24"', text)
        self.assertIn('[3] noop   dcim/devices #11 "sw2"', text)
        self.assertIn("Summary: 1 create, 1 update, 1 delete, 1 noop", text)


if __name__ == "__main__":
    unittest.main()
