import json

from .base import PluginTestCase


def _ops():
    return [
        {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}},
        {"op": "create", "endpoint": "dcim/sites", "data": {"name": "Berlin", "slug": "ber"}},
        {"op": "delete", "endpoint": "ipam/ip-addresses", "id": 21},
    ]


class ChangelogLink(PluginTestCase):
    def call(self, name, args, **kwargs):
        return json.loads(self.handlers.HANDLERS[name](args, **kwargs))

    def changelog_gets(self):
        return [c for c in self.nb.calls if c["method"] == "GET" and c["endpoint"].endswith("object-changes")]

    def test_apply_links_each_write_to_a_change_record(self):
        out = self.call("netbox_plan", {"description": "d", "operations": _ops()})
        applied = self.call("netbox_apply", {"plan_id": out["plan_id"]})
        plan = self.store.load(out["plan_id"])
        self.assertEqual(plan["apply"]["changelog"]["linked"], True)
        self.assertEqual((plan["apply"]["changelog"]["matched"], plan["apply"]["changelog"]["unmatched"]), (3, 0))
        types = ("dcim.device", "dcim.site", "ipam.ipaddresse")
        for entry, ctype in zip(plan["journal"], types):
            self.assertEqual(len(entry["netbox_changes"]), 1, entry)
            change = entry["netbox_changes"][0]
            self.assertEqual(change["changed_object_type"], ctype)
            self.assertTrue(change["request_id"])
            self.assertIsInstance(change["id"], int)
        gets = self.changelog_gets()
        self.assertEqual([g["endpoint"] for g in gets], ["core/object-changes"])
        self.assertTrue(gets[0]["params"]["time_after"] < gets[0]["params"]["time_before"])
        self.assertEqual([len(e["netbox_changes"]) for e in applied["journal"]], [1, 1, 1])
        self.assertEqual(applied["changelog"]["matched"], 3)
        linked = [e for e in self.events() if e["event"] == "changelog_linked"]
        self.assertEqual((linked[-1]["phase"], linked[-1]["matched"], len(linked[-1]["request_ids"])), ("apply", 3, 3))

    def test_rollback_links_the_inverse_writes(self):
        out = self.call("netbox_plan", {"description": "d", "operations": _ops()})
        self.call("netbox_apply", {"plan_id": out["plan_id"]})
        rolled = self.call("netbox_rollback", {"plan_id": out["plan_id"]})
        plan = self.store.load(out["plan_id"])
        self.assertEqual(plan["rollback"]["changelog"]["matched"], 3)
        undo_update, undo_create, undo_delete = plan["journal"]
        self.assertEqual(undo_update["revert_netbox_changes"][0]["changed_object_type"], "dcim.device")
        self.assertEqual(undo_create["revert_netbox_changes"][0]["changed_object_type"], "dcim.site")
        recreated = undo_delete["revert"]["new_object_id"]
        row = self.nb.get(self.nb.changelog_endpoint, undo_delete["revert_netbox_changes"][0]["id"])
        self.assertEqual(row["changed_object_id"], recreated)
        self.assertEqual([len(e["revert_netbox_changes"]) for e in rolled["journal"]], [1, 1, 1])
        self.assertEqual(rolled["rollback"]["changelog"]["linked"], True)
        phases = [e["phase"] for e in self.events() if e["event"] == "changelog_linked"]
        self.assertEqual(phases, ["apply", "rollback"])

    def test_forbidden_log_is_recorded_and_never_fails_the_apply(self):
        self.nb.changelog_forbidden = True
        out = self.call("netbox_plan", {"description": "d", "operations": _ops()})
        applied = self.call("netbox_apply", {"plan_id": out["plan_id"]})
        self.assertEqual(applied["outcome"], "applied")
        self.assertEqual(applied["changelog"]["linked"], False)
        self.assertEqual(applied["changelog"]["status"], 403)
        plan = self.store.load(out["plan_id"])
        self.assertNotIn("netbox_changes", plan["journal"][0])
        linked = [e for e in self.events() if e["event"] == "changelog_linked"][-1]
        self.assertEqual((linked["linked"], linked["http_status"]), (False, 403))

    def test_falls_back_to_the_3x_endpoint(self):
        self.nb.changelog_endpoint = "extras/object-changes"
        out = self.call("netbox_plan", {"description": "d", "operations": _ops()[:1]})
        self.call("netbox_apply", {"plan_id": out["plan_id"]})
        self.assertEqual(
            [g["endpoint"] for g in self.changelog_gets()], ["core/object-changes", "extras/object-changes"]
        )
        self.assertEqual(self.store.load(out["plan_id"])["apply"]["changelog"]["matched"], 1)

    def test_setting_off_skips_the_lookup(self):
        self.settings_mod.set_settings(self.settings_mod.Settings(allow_delete=True, link_changelog=False))
        out = self.call("netbox_plan", {"description": "d", "operations": _ops()[:1]})
        applied = self.call("netbox_apply", {"plan_id": out["plan_id"]})
        self.assertEqual(self.changelog_gets(), [])
        self.assertIsNone(applied["changelog"])
        self.assertNotIn("netbox_changes", self.store.load(out["plan_id"])["journal"][0])

    def test_revert_that_wrote_nothing_is_not_counted(self):
        out = self.call(
            "netbox_plan",
            {
                "description": "d",
                "operations": [{"op": "create", "endpoint": "dcim/sites", "data": {"name": "B", "slug": "b"}}],
            },
        )
        self.call("netbox_apply", {"plan_id": out["plan_id"]})
        new_id = self.store.load(out["plan_id"])["journal"][0]["object_id"]
        del self.nb.tables["dcim/sites"][new_id]
        rolled = self.call("netbox_rollback", {"plan_id": out["plan_id"]})
        summary = rolled["rollback"]["changelog"]
        self.assertEqual((summary["linked"], summary["matched"], summary["unmatched"]), (True, 0, 0))
