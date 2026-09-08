import unittest
from datetime import datetime, timedelta, timezone

from .base import PluginTestCase
from .fake_netbox import _Response


def _ops_mixed():
    return [
        {
            "op": "update",
            "endpoint": "dcim/devices",
            "id": 10,
            "data": {"status": "planned", "custom_fields": {"tier": 2}},
        },
        {
            "op": "create",
            "endpoint": "dcim/devices",
            "match": {"name": "sw8"},
            "data": {"name": "sw8", "site": 1, "role": 1, "device_type": 1, "status": "planned"},
        },
        {"op": "delete", "endpoint": "ipam/ip-addresses", "id": 20},
        {"op": "update", "endpoint": "dcim/devices", "id": 11, "data": {"status": "planned"}},  # noop
    ]


class Apply(PluginTestCase):
    def test_full_apply_journals_inverses(self):
        plan = self.plan(_ops_mixed(), "mixed")
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["apply"]["outcome"], "applied")
        statuses = [e["status"] for e in result["journal"]]
        self.assertEqual(statuses, ["done", "done", "done", "skipped"])
        # effects
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "planned")
        self.assertEqual(self.nb.get("dcim/devices", 10)["custom_fields"], {"owner": "netops", "tier": 2})
        new = [d for d in self.nb.tables["dcim/devices"].values() if d["name"] == "sw8"]
        self.assertEqual(len(new), 1)
        self.assertIsNone(self.nb.get("ipam/ip-addresses", 20))
        # inverses
        j = result["journal"]
        self.assertEqual(
            j[0]["inverse"],
            {
                "action": "update",
                "endpoint": "dcim/devices",
                "object_id": 10,
                "data": {"status": "active", "custom_fields": {"tier": 1}},
            },
        )
        self.assertEqual(j[0]["after_last_updated"], self.nb.get("dcim/devices", 10)["last_updated"])
        self.assertEqual(j[1]["inverse"], {"action": "delete", "endpoint": "dcim/devices", "object_id": new[0]["id"]})
        self.assertEqual(j[2]["inverse"]["action"], "create")
        self.assertEqual(j[2]["inverse"]["snapshot"]["address"], "10.0.0.1/24")
        # persisted
        self.assertEqual(self.store.load(plan["id"])["status"], "applied")
        self.assertEqual(len(self.nb.writes()), 3)

    def test_apply_refuses_twice(self):
        plan = self.plan(_ops_mixed())
        self.executor.apply_plan(self.client, plan, self.store, self.settings)
        with self.assertRaises(self.executor.ExecutionRefused):
            self.executor.apply_plan(self.client, plan, self.store, self.settings)

    def test_preflight_refusals(self):
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "x"}}])
        old = dict(plan, created_at=(datetime.now(timezone.utc) - timedelta(hours=30)).isoformat())
        with self.assertRaises(self.executor.ExecutionRefused) as ctx:
            self.executor.apply_plan(self.client, old, self.store, self.settings)
        self.assertIn("max_plan_age_hours", str(ctx.exception))
        other = dict(plan, netbox_url="https://other.example")
        with self.assertRaises(self.executor.ExecutionRefused) as ctx:
            self.executor.apply_plan(self.client, other, self.store, self.settings)
        self.assertIn("NETBOX_URL is now", str(ctx.exception))
        dplan = self.plan([{"op": "delete", "endpoint": "ipam/ip-addresses", "id": 21}])
        with self.assertRaises(self.executor.ExecutionRefused) as ctx:
            self.executor.apply_plan(self.client, dplan, self.store, self.settings_mod.Settings(allow_delete=False))
        self.assertIn("allow_delete", str(ctx.exception))
        self.assertEqual(len(self.nb.writes()), 0)

    def test_conflict_when_object_changed_since_planning(self):
        plan = self.plan(
            [
                {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "A"}},
                {"op": "update", "endpoint": "dcim/devices", "id": 11, "data": {"serial": "B"}},
            ]
        )
        self.nb.touch("dcim/devices", 11)
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["apply"]["failure"]["kind"], "conflict")
        self.assertEqual(result["apply"]["failed_step"], 1)
        self.assertEqual(result["journal"][0]["revert_status"], "reverted")
        self.assertEqual(result["journal"][1]["status"], "conflict")
        self.assertEqual(self.nb.get("dcim/devices", 10)["serial"], "")
        self.assertIn("automatic after failure at step 1", result["rollback"]["reason"])

    def test_conflict_when_upsert_target_appeared(self):
        plan = self.plan(
            [
                {
                    "op": "create",
                    "endpoint": "dcim/devices",
                    "match": {"name": "sw8"},
                    "data": {"name": "sw8", "site": 1, "role": 1, "device_type": 1},
                }
            ]
        )
        self.nb.seed("dcim/devices", {"name": "sw8", "site": {"id": 1, "slug": "nyc"}})
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertEqual(result["status"], "failed")  # nothing to roll back
        self.assertIn("now match", result["journal"][0]["error"])
        self.assertNotIn("rollback", result)

    def test_conflict_when_target_deleted(self):
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 12, "data": {"serial": "x"}}])
        del self.nb.tables["dcim/devices"][12]
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertEqual(result["journal"][0]["status"], "conflict")
        self.assertIn("no longer exists", result["journal"][0]["error"])

    def test_failure_midway_rolls_back_in_reverse(self):
        plan = self.plan(_ops_mixed())
        # third write (the DELETE of the IP) blows up; rollback's own DELETE of the new device must still work
        self.nb.fail_on = lambda m, e, b: (
            _Response(500, {"detail": "db down"}) if (m, e) == ("DELETE", "ipam/ip-addresses") else None
        )
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["apply"]["failed_step"], 2)
        j = result["journal"]
        self.assertEqual([e["status"] for e in j], ["done", "done", "failed"])
        self.assertEqual(j[2]["netbox"]["status"], 500)
        self.assertEqual([e.get("revert_status") for e in j], ["reverted", "reverted", None])
        # reverse order: the create was reverted before the update
        self.assertLess(j[1]["revert_at"], j[0]["revert_at"] + "~")
        reverts = [c for c in self.nb.calls if c["method"] in {"DELETE", "PATCH"}][-2:]
        self.assertEqual([c["method"] for c in reverts], ["DELETE", "PATCH"])
        # state restored
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "active")
        self.assertEqual(self.nb.get("dcim/devices", 10)["custom_fields"]["tier"], 1)
        self.assertFalse([d for d in self.nb.tables["dcim/devices"].values() if d["name"] == "sw8"])
        self.assertIsNotNone(self.nb.get("ipam/ip-addresses", 20))

    def test_failure_without_auto_rollback_leaves_journal_for_manual_rollback(self):
        plan = self.plan(_ops_mixed())
        self.nb.fail_on = lambda m, e, b: (
            _Response(500, {"detail": "db down"}) if (m, e) == ("DELETE", "ipam/ip-addresses") else None
        )
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings, rollback_on_failure=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "planned")
        self.nb.fail_on = None
        rolled = self.executor.rollback_plan(self.client, self.store.load(plan["id"]), self.store)
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "active")

    def test_dry_run_writes_nothing_and_reports_conflicts(self):
        plan = self.plan(_ops_mixed())
        self.nb.touch("dcim/devices", 10)
        result = self.executor.apply_plan(self.client, plan, self.store, self.settings, dry_run=True)
        self.assertEqual(result["status"], "planned")
        self.assertEqual([c["index"] for c in result["checks"][-1]["conflicts"]], [0])
        self.assertEqual(len(self.nb.writes()), 0)
        self.assertEqual(self.store.load(plan["id"])["status"], "planned")


class Rollback(PluginTestCase):
    def applied(self, ops=None):
        plan = self.plan(ops or _ops_mixed())
        return self.executor.apply_plan(self.client, plan, self.store, self.settings)

    def test_rollback_restores_everything(self):
        plan = self.applied()
        before_ids = set(self.nb.tables["dcim/devices"])
        result = self.executor.rollback_plan(self.client, plan, self.store, reason="requested")
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["rollback"]["reverted"], 3)
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "active")
        self.assertEqual(self.nb.get("dcim/devices", 10)["custom_fields"]["tier"], 1)
        self.assertEqual(set(self.nb.tables["dcim/devices"]), before_ids - {max(before_ids)})
        recreated = [ip for ip in self.nb.tables["ipam/ip-addresses"].values() if ip["address"] == "10.0.0.1/24"]
        self.assertEqual(len(recreated), 1)
        self.assertNotEqual(recreated[0]["id"], 20)  # new id, as documented
        entry = result["journal"][2]
        self.assertEqual(entry["revert"]["new_object_id"], recreated[0]["id"])
        self.assertEqual(self.store.load(plan["id"])["status"], "rolled_back")

    def test_rollback_skips_objects_modified_after_apply_unless_forced(self):
        plan = self.applied()
        self.nb.touch("dcim/devices", 10)
        result = self.executor.rollback_plan(self.client, plan, self.store)
        self.assertEqual(result["status"], "partially_rolled_back")
        self.assertEqual(result["journal"][0]["revert_status"], "conflict")
        self.assertIn("modified after apply", result["journal"][0]["revert"]["error"])
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "planned")
        # retry with force finishes the job and only touches the outstanding entry
        writes_before = len(self.nb.writes())
        forced = self.executor.rollback_plan(self.client, result, self.store, force=True)
        self.assertEqual(forced["status"], "rolled_back")
        self.assertEqual(len(self.nb.writes()) - writes_before, 1)
        self.assertEqual(self.nb.get("dcim/devices", 10)["status"]["value"], "active")

    def test_rollback_of_create_when_object_already_gone(self):
        plan = self.applied([{"op": "create", "endpoint": "dcim/sites", "data": {"name": "Berlin", "slug": "ber"}}])
        new_id = plan["journal"][0]["object_id"]
        del self.nb.tables["dcim/sites"][new_id]
        result = self.executor.rollback_plan(self.client, plan, self.store)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["journal"][0]["revert"]["note"], "object already gone")

    def test_rollback_of_update_when_object_deleted_is_failed(self):
        plan = self.applied([{"op": "update", "endpoint": "dcim/devices", "id": 12, "data": {"serial": "Z"}}])
        del self.nb.tables["dcim/devices"][12]
        result = self.executor.rollback_plan(self.client, plan, self.store)
        self.assertEqual(result["status"], "partially_rolled_back")
        self.assertEqual(result["journal"][0]["revert_status"], "failed")

    def test_recreate_drops_fields_netbox_rejects(self):
        plan = self.applied([{"op": "delete", "endpoint": "dcim/devices", "id": 12}])
        self.nb.reject_fields["dcim/devices"] = ["primary_ip4"]
        result = self.executor.rollback_plan(self.client, plan, self.store)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(result["journal"][0]["revert"]["dropped_fields"], ["primary_ip4"])
        recreated = [d for d in self.nb.tables["dcim/devices"].values() if d["name"] == "sw3"]
        self.assertEqual(len(recreated), 1)
        self.assertNotIn("primary_ip4", recreated[0])
        self.assertEqual(recreated[0]["site"]["slug"], "sfo")

    def test_rollback_refused_for_planned_or_rolled_back(self):
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "x"}}])
        with self.assertRaises(self.executor.ExecutionRefused):
            self.executor.rollback_plan(self.client, plan, self.store)
        done = self.executor.rollback_plan(
            self.client, self.executor.apply_plan(self.client, plan, self.store, self.settings), self.store
        )
        with self.assertRaises(self.executor.ExecutionRefused):
            self.executor.rollback_plan(self.client, done, self.store)

    def test_journal_render(self):
        plan = self.applied()
        text = self.executor.render_journal(
            self.executor.rollback_plan(self.client, plan, self.store, reason="requested")
        )
        self.assertIn("[0] update dcim/devices #10 -> done | rollback: reverted", text)
        self.assertIn("re-created as #", text)
        self.assertIn("Rollback: 3 reverted, 0 conflicts, 0 failed (requested)", text)


if __name__ == "__main__":
    unittest.main()
