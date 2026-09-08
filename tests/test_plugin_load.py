"""Load the plugin through Hermes' real ``PluginManager`` when a Hermes checkout is importable.

Set ``HERMES_AGENT_ROOT`` (or run from an environment where ``hermes_cli`` imports) to enable.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _hermes_importable() -> bool:
    root = os.environ.get("HERMES_AGENT_ROOT")
    if root and root not in sys.path:
        sys.path.insert(0, root)
    return importlib.util.find_spec("hermes_cli") is not None


@unittest.skipUnless(_hermes_importable(), "hermes_cli not importable (set HERMES_AGENT_ROOT)")
class LoadThroughPluginManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = Path(self.tmp) / "hermes-home"
        (self.home / "plugins").mkdir(parents=True)
        shutil.copytree(
            REPO_ROOT,
            self.home / "plugins" / "netbox",
            ignore=shutil.ignore_patterns("tests", ".git", "__pycache__", ".github"),
        )
        (self.home / "config.yaml").write_text(
            "plugins:\n  enabled: [netbox]\n  entries:\n    netbox:\n      settings:\n        allow_delete: true\n"
            "        max_operations: 7\n"
        )
        self._env = dict(os.environ)
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["NETBOX_URL"] = "https://netbox.test"
        os.environ["NETBOX_TOKEN"] = "nbt_a.b"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registration_through_real_loader(self):
        from hermes_cli.plugins import PluginManager
        from tools.registry import registry

        mgr = PluginManager()
        mgr.discover_and_load()
        loaded = {p["name"]: p for p in mgr.list_plugins()}
        self.assertIn("netbox", loaded, loaded)
        self.assertIsNone(loaded["netbox"]["error"])
        self.assertEqual(loaded["netbox"]["tools"], 5)
        self.assertGreaterEqual(loaded["netbox"]["commands"], 1)
        for name in ("netbox_query", "netbox_plan", "netbox_apply", "netbox_rollback", "netbox_plans"):
            self.assertIsNotNone(registry.get_schema(name), name)
            self.assertEqual(registry.get_toolset_for_tool(name), "netbox")
        self.assertIsNotNone(mgr.find_plugin_skill("netbox:workflow"))

        # settings flowed from config.yaml into the plugin
        mod = sys.modules["hermes_plugins.netbox"]
        self.assertEqual(mod.settings.get_settings().max_operations, 7)
        self.assertTrue(mod.settings.get_settings().allow_delete)

        # a dispatched call goes through the registry and comes back as structured JSON
        import json

        out = json.loads(registry.dispatch("netbox_plans", {}))
        self.assertTrue(out["success"])
        self.assertEqual(out["count"], 0)


if __name__ == "__main__":
    unittest.main()
