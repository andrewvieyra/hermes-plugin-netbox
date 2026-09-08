import json
import tempfile
import unittest
from pathlib import Path

from .helpers import submodule

store_mod = submodule("store")


def _plan(pid: str) -> dict:
    return {"id": pid, "status": "planned", "created_at": store_mod.now_iso(), "steps": [], "journal": []}


class ExclusiveCreate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.PlanStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_never_overwrites_an_existing_plan(self):
        first = _plan("nbp-20260908T190000Z-aaaa")
        first["marker"] = "first"
        self.store.create(first)
        second = _plan("nbp-20260908T190000Z-aaaa")  # same id, as if the random suffix repeated
        second["marker"] = "second"
        self.store.create(second)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.store.load(first["id"])["marker"], "first")
        self.assertEqual(self.store.load(second["id"])["marker"], "second")
        self.assertEqual(len(self.store.list()), 2)

    def test_create_gives_up_after_attempts(self):
        original = store_mod.new_plan_id
        store_mod.new_plan_id = lambda: "nbp-20260908T190000Z-bbbb"
        try:
            self.store.create(_plan("nbp-20260908T190000Z-bbbb"))
            with self.assertRaises(RuntimeError):
                self.store.create(_plan("nbp-20260908T190000Z-bbbb"), attempts=3)
        finally:
            store_mod.new_plan_id = original

    def test_created_file_is_private_and_valid_json(self):
        plan = _plan("nbp-20260908T190000Z-cccc")
        self.store.create(plan)
        path = Path(self.tmp.name) / f"{plan['id']}.json"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(path.read_text())["id"], plan["id"])
        self.assertIn("updated_at", json.loads(path.read_text()))


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.PlanStore(Path(self.tmp.name))
        for pid in ("nbp-20260908T190000Z-4f1a", "nbp-20260908T190100Z-9c2e", "nbp-20260908T190200Z-9c2f"):
            self.store.create(_plan(pid))

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_suffix_substring_and_case(self):
        self.assertEqual(self.store.resolve("nbp-20260908T190000Z-4f1a")[0], "nbp-20260908T190000Z-4f1a")
        self.assertEqual(self.store.resolve("4f1a")[0], "nbp-20260908T190000Z-4f1a")
        self.assertEqual(self.store.resolve("4F1A")[0], "nbp-20260908T190000Z-4f1a")
        self.assertEqual(self.store.resolve("T190000Z-4f1a")[0], "nbp-20260908T190000Z-4f1a")
        self.assertEqual(self.store.resolve("190100")[0], "nbp-20260908T190100Z-9c2e")

    def test_ambiguous_and_missing(self):
        pid, candidates = self.store.resolve("9c2")
        self.assertIsNone(pid)
        self.assertEqual(candidates, ["nbp-20260908T190100Z-9c2e", "nbp-20260908T190200Z-9c2f"])
        self.assertEqual(self.store.resolve("zzzz"), (None, []))
        self.assertEqual(self.store.resolve(""), (None, []))
        self.assertEqual(self.store.resolve("../etc"), (None, []))


if __name__ == "__main__":
    unittest.main()
