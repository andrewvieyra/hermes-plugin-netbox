from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .fake_netbox import FakeNetBox, seeded
from .helpers import submodule


class PluginTestCase(unittest.TestCase):
    """Fresh fake NetBox, client, temp plan store and default settings for every test."""

    def setUp(self) -> None:
        self.client_mod = submodule("client")
        self.store_mod = submodule("store")
        self.settings_mod = submodule("settings")
        self.planner = submodule("planner")
        self.executor = submodule("executor")
        self.handlers = submodule("handlers")
        self.diff = submodule("diff")
        self.nb: FakeNetBox = seeded()
        self.client = self.client_mod.NetBoxClient(self.nb.base_url, "nbt_abc.def", session=self.nb)
        self._tmp = tempfile.TemporaryDirectory()
        self.store = self.store_mod.PlanStore(Path(self._tmp.name))
        self.store_mod.set_store(self.store)
        self.settings = self.settings_mod.Settings(allow_delete=True)
        self.settings_mod.set_settings(self.settings)
        self.handlers.set_client_factory(lambda: self.client)
        self.audit = submodule("audit")
        self.audit_path = Path(self._tmp.name) / "audit.jsonl"
        self.audit.set_audit_log(self.audit.AuditLog(self.audit_path))

    def tearDown(self) -> None:
        self.audit.set_audit_log(None)
        self.handlers.set_client_factory(None)
        self.store_mod.set_store(None)
        self.settings_mod.set_settings(self.settings_mod.Settings())
        self._tmp.cleanup()

    def plan(self, operations, description="test"):
        plan = self.planner.build_plan(self.client, operations, description, self.settings)
        self.store.create(plan)
        return plan

    def events(self):
        """Parsed audit records written so far, in order."""
        import json

        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line]
