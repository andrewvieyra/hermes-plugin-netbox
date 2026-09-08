import argparse
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from .base import PluginTestCase
from .fake_netbox import _Response
from .helpers import submodule

_SESSION = {
    "HERMES_SESSION_PLATFORM": "signal",
    "HERMES_SESSION_SOURCE": "gateway",
    "HERMES_SESSION_CHAT_ID": "+15550001111",
    "HERMES_SESSION_CHAT_NAME": "Andrew",
    "HERMES_SESSION_CHAT_TYPE": "dm",
    "HERMES_SESSION_USER_ID": "+15550001111",
    "HERMES_SESSION_USER_ID_ALT": "uuid-1234",
    "HERMES_SESSION_USER_NAME": "Andrew",
    "HERMES_SESSION_KEY": "signal:dm:+15550001111",
    "HERMES_SESSION_ID": "sess-abc",
    "HERMES_SESSION_MESSAGE_ID": "m-77",
}


class _EnvMixin:
    def set_session(self, **overrides):
        self._saved = {k: os.environ.get(k) for k in list(_SESSION) + ["HERMES_CRON_SESSION"] + list(overrides)}
        os.environ.update(_SESSION)
        os.environ.update(overrides)

    def clear_session(self):
        for k, v in getattr(self, "_saved", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ActorCapture(_EnvMixin, PluginTestCase):
    def tearDown(self):
        self.clear_session()
        super().tearDown()

    def test_model_actor_from_session_context_and_kwargs(self):
        self.set_session()
        actor = self.audit.capture_actor(
            {"task_id": "t-1", "session_id": "sess-override", "user_task": "Move sw1 to SFO please"}, via="tool"
        )
        self.assertEqual(actor["kind"], "model")
        self.assertEqual(actor["via"], "tool")
        self.assertEqual(actor["platform"], "signal")
        self.assertEqual(actor["user_id"], "+15550001111")
        self.assertEqual(actor["user_name"], "Andrew")
        self.assertEqual(actor["chat_type"], "dm")
        self.assertEqual(actor["session_id"], "sess-override")  # explicit kwarg wins over context
        self.assertEqual(actor["task_id"], "t-1")
        self.assertEqual(actor["message_id"], "m-77")
        self.assertFalse(actor["cron"])
        self.assertEqual(actor["request"], "Move sw1 to SFO please")
        self.assertTrue(actor["os_user"])

    def test_operator_actor_and_missing_context_is_null_not_absent(self):
        actor = self.audit.capture_actor({}, via="cli")
        self.assertEqual((actor["kind"], actor["via"]), ("operator", "cli"))
        for key in ("platform", "user_id", "chat_id", "session_id", "task_id", "request"):
            self.assertIn(key, actor)
            self.assertIsNone(actor[key])

    def test_request_text_truncated_and_optional(self):
        long = "x" * 900
        actor = self.audit.capture_actor({"user_task": long})
        self.assertEqual(len(actor["request"]), 500)
        self.assertTrue(actor["request"].endswith("…"))
        self.settings_mod.set_settings(self.settings_mod.Settings(allow_delete=True, audit_include_request=False))
        self.assertIsNone(self.audit.capture_actor({"user_task": "secret ask"})["request"])

    def test_cron_flag(self):
        self.set_session(HERMES_CRON_SESSION="1")
        self.assertTrue(self.audit.capture_actor({})["cron"])


class PlanFileAudit(_EnvMixin, PluginTestCase):
    def tearDown(self):
        self.clear_session()
        super().tearDown()

    def call(self, name, args, **kwargs):
        return json.loads(self.handlers.HANDLERS[name](args, **kwargs))

    def test_plan_records_requester_environment_and_netbox_version(self):
        self.set_session()
        out = self.call(
            "netbox_plan",
            {
                "description": "d",
                "operations": [{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}],
            },
            task_id="t-9",
            session_id="sess-abc",
            user_task="set the serial",
        )
        plan = self.store.load(out["plan_id"])
        self.assertEqual(plan["requested_by"]["kind"], "model")
        self.assertEqual(plan["requested_by"]["platform"], "signal")
        self.assertEqual(plan["requested_by"]["task_id"], "t-9")
        self.assertEqual(plan["requested_by"]["request"], "set the serial")
        self.assertEqual(plan["audit"]["netbox_version"], "4.3.0")
        self.assertEqual(plan["audit"]["plugin_version"], submodule("version").__version__)
        for key in ("host", "os_user", "pid", "hermes_version", "hermes_home"):
            self.assertIn(key, plan["audit"])

    def test_apply_and_rollback_record_actor_and_http_requests(self):
        out = self.call(
            "netbox_plan",
            {
                "description": "d",
                "operations": [
                    {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}},
                    {"op": "create", "endpoint": "dcim/sites", "data": {"name": "Berlin", "slug": "ber"}},
                    {"op": "delete", "endpoint": "ipam/ip-addresses", "id": 21},
                ],
            },
        )
        pid = out["plan_id"]
        self.call("netbox_apply", {"plan_id": pid}, task_id="t-apply")
        plan = self.store.load(pid)
        self.assertEqual(plan["apply"]["actor"]["kind"], "model")
        self.assertEqual(plan["apply"]["actor"]["task_id"], "t-apply")
        reqs = [e["request"] for e in plan["journal"]]
        self.assertEqual(reqs[0], {"method": "PATCH", "path": "/api/dcim/devices/10/", "status": 200})
        self.assertEqual((reqs[1]["method"], reqs[1]["status"]), ("POST", 201))
        self.assertEqual(reqs[2], {"method": "DELETE", "path": "/api/ipam/ip-addresses/21/", "status": 204})
        self.call("netbox_rollback", {"plan_id": pid}, _actor=self.audit.capture_actor({}, via="slash"))
        plan = self.store.load(pid)
        self.assertEqual(plan["rollback"]["actor"]["via"], "slash")
        self.assertEqual(plan["rollback"]["actor"]["kind"], "operator")
        self.assertEqual(plan["journal"][2]["revert_request"]["method"], "POST")


class EventStream(_EnvMixin, PluginTestCase):
    def tearDown(self):
        self.clear_session()
        super().tearDown()

    def call(self, name, args, **kwargs):
        return json.loads(self.handlers.HANDLERS[name](args, **kwargs))

    def test_full_lifecycle_sequence_and_stable_keys(self):
        self.set_session()
        out = self.call(
            "netbox_plan",
            {
                "description": "lifecycle",
                "operations": [
                    {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}},
                    {"op": "update", "endpoint": "dcim/devices", "id": 11, "data": {"status": "planned"}},
                ],
            },
            user_task="lifecycle test",
        )
        pid = out["plan_id"]
        self.call("netbox_apply", {"plan_id": pid, "dry_run": True})
        self.call("netbox_apply", {"plan_id": pid})
        self.call("netbox_rollback", {"plan_id": pid})
        events = self.events()
        self.assertEqual(
            [e["event"] for e in events],
            [
                "plan_created",
                "plan_checked",
                "apply_started",
                "step_done",
                "step_skipped",
                "changelog_linked",
                "apply_finished",
                "rollback_started",
                "revert_reverted",
                "changelog_linked",
                "rollback_finished",
            ],
        )
        base_keys = {"ts", "schema", "plugin", "event", "plan_id", "netbox_url", "actor", "host", "pid"}
        for e in events:
            self.assertTrue(base_keys <= set(e), e)
            self.assertEqual(e["plan_id"], pid)
            self.assertEqual(e["schema"], 1)
            self.assertTrue(e["ts"].endswith("Z"))
            self.assertEqual(e["actor"]["platform"], "signal")
        created = events[0]
        self.assertEqual(created["summary"], {"create": 0, "update": 1, "delete": 0, "noop": 1})
        self.assertEqual(created["actor"]["request"], "lifecycle test")
        step = events[3]
        self.assertEqual(
            (step["index"], step["action"], step["object_id"], step["http_status"]), (0, "update", 10, 200)
        )
        self.assertEqual(events[6]["outcome"], "applied")
        self.assertEqual(events[10]["reverted"], 1)

    def test_failure_emits_step_failed_and_automatic_rollback(self):
        out = self.call(
            "netbox_plan",
            {
                "description": "d",
                "operations": [
                    {"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}},
                    {"op": "delete", "endpoint": "ipam/ip-addresses", "id": 21},
                ],
            },
        )
        self.nb.fail_on = lambda m, e, b: (
            _Response(500, {"detail": "db down"}) if (m, e) == ("DELETE", "ipam/ip-addresses") else None
        )
        self.call("netbox_apply", {"plan_id": out["plan_id"]})
        names = [e["event"] for e in self.events()]
        self.assertEqual(
            names,
            [
                "plan_created",
                "apply_started",
                "step_done",
                "step_failed",
                "changelog_linked",
                "apply_finished",
                "rollback_started",
                "revert_reverted",
                "changelog_linked",
                "rollback_finished",
            ],
        )
        failed = self.events()[3]
        self.assertEqual((failed["http_status"], failed["request"]["method"]), (500, "DELETE"))
        self.assertEqual(self.events()[5]["outcome"], "failed")
        self.assertIn("automatic", self.events()[6]["reason"])

    def test_refusals_and_rejections_are_audited(self):
        self.call("netbox_plan", {"description": "bad", "operations": [{"op": "update", "endpoint": "dcim/devices"}]})
        self.call("netbox_apply", {"plan_id": "nbp-nope"})
        out = self.call(
            "netbox_plan",
            {
                "description": "d",
                "operations": [{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}],
            },
        )
        self.call("netbox_apply", {"plan_id": out["plan_id"]})
        self.call("netbox_apply", {"plan_id": out["plan_id"]})  # second apply refused
        self.call("netbox_rollback", {"plan_id": "nbp-nope"})
        names = [e["event"] for e in self.events()]
        self.assertEqual(names[0], "plan_rejected")
        self.assertEqual(self.events()[0]["errors"], 2)  # missing target and missing data
        self.assertEqual(names[1], "apply_refused")
        self.assertEqual(self.events()[1]["reason"], "unknown plan_id")
        self.assertEqual(names[-2], "apply_refused")
        self.assertIn("only a 'planned' plan can be applied", self.events()[-2]["reason"])
        self.assertEqual(names[-1], "rollback_refused")

    def test_operator_paths_are_labelled(self):
        commands = submodule("commands")
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        commands.slash_handler(f"apply {plan['id']}")
        parser = argparse.ArgumentParser()
        commands.setup_cli(parser)
        with redirect_stdout(io.StringIO()):
            commands.cli_handler(parser.parse_args(["rollback", plan["id"]]))
        started = [e for e in self.events() if e["event"] in {"apply_started", "rollback_started"}]
        self.assertEqual(
            [(e["actor"]["kind"], e["actor"]["via"]) for e in started], [("operator", "slash"), ("operator", "cli")]
        )

    def test_disabled_log_writes_nothing(self):
        self.audit.set_audit_log(self.audit.AuditLog(self.audit_path, enabled=False))
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertFalse(self.audit_path.exists())

    def test_unwritable_log_never_breaks_the_operation(self):
        self.audit.set_audit_log(self.audit.AuditLog(Path(self._tmp.name)))  # a directory, not a file
        plan = self.plan([{"op": "update", "endpoint": "dcim/devices", "id": 10, "data": {"serial": "S"}}])
        with self.assertLogs("hermes_plugins.netbox.audit", level="WARNING"):
            result = self.executor.apply_plan(self.client, plan, self.store, self.settings)
        self.assertEqual(result["status"], "applied")

    def test_default_path_and_settings_override(self):
        self.audit.set_audit_log(None)
        self.settings_mod.set_settings(
            self.settings_mod.Settings(audit_log_path=str(Path(self._tmp.name) / "custom.jsonl"))
        )
        log = self.audit.get_audit_log()
        self.assertEqual(log.path.name, "custom.jsonl")
        self.audit.set_audit_log(None)
        self.settings_mod.set_settings(self.settings_mod.Settings(audit_log=False))
        self.assertFalse(self.audit.get_audit_log().enabled)


if __name__ == "__main__":
    unittest.main()
