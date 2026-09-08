import unittest

from .helpers import submodule

diff = submodule("diff")


class ValuesEqual(unittest.TestCase):
    site = {"id": 1, "url": "u", "display": "New York", "name": "New York", "slug": "nyc"}
    choice = {"value": "active", "label": "Active"}

    def test_nested_object_by_id(self):
        self.assertTrue(diff.values_equal(1, self.site))
        self.assertFalse(diff.values_equal(2, self.site))

    def test_nested_object_by_slug_or_name(self):
        self.assertTrue(diff.values_equal("nyc", self.site))
        self.assertTrue(diff.values_equal("New York", self.site))
        self.assertFalse(diff.values_equal("sfo", self.site))

    def test_nested_object_by_lookup_dict(self):
        self.assertTrue(diff.values_equal({"slug": "nyc"}, self.site))
        self.assertTrue(diff.values_equal({"id": 1}, self.site))
        self.assertFalse(diff.values_equal({"slug": "sfo"}, self.site))
        self.assertFalse(diff.values_equal({}, self.site))

    def test_bool_never_matches_object(self):
        self.assertFalse(diff.values_equal(True, self.site))

    def test_choice_field(self):
        self.assertTrue(diff.values_equal("active", self.choice))
        self.assertTrue(diff.values_equal({"value": "active"}, self.choice))
        self.assertFalse(diff.values_equal("planned", self.choice))

    def test_lists_as_multisets(self):
        tags = [{"id": 1, "name": "core", "slug": "core"}, {"id": 2, "name": "edge", "slug": "edge"}]
        self.assertTrue(diff.values_equal([2, 1], tags))
        self.assertTrue(diff.values_equal(["edge", "core"], tags))
        self.assertFalse(diff.values_equal([1], tags))
        self.assertFalse(diff.values_equal([1, 1], tags))
        self.assertFalse(diff.values_equal("core", tags))

    def test_plain_dict_deep_equality(self):
        self.assertTrue(diff.values_equal({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}))
        self.assertFalse(diff.values_equal({"a": 1}, {"a": 1, "b": 2}))

    def test_scalars_with_numeric_string_leniency(self):
        self.assertTrue(diff.values_equal("1", 1))
        self.assertTrue(diff.values_equal(1.0, 1))
        self.assertFalse(diff.values_equal(None, ""))
        self.assertFalse(diff.values_equal(True, "true"))
        self.assertTrue(diff.values_equal(None, None))


class ToWritable(unittest.TestCase):
    def test_conversions(self):
        obj = {
            "site": {"id": 3, "name": "x"},
            "status": {"value": "active", "label": "Active"},
            "tags": [{"id": 1}, {"id": 2}],
            "custom_fields": {"owner": {"id": 9, "name": "team"}, "tier": 1},
            "serial": "abc",
            "flag": None,
        }
        self.assertEqual(
            diff.to_writable(obj),
            {
                "site": 3,
                "status": "active",
                "tags": [1, 2],
                "custom_fields": {"owner": 9, "tier": 1},
                "serial": "abc",
                "flag": None,
            },
        )


class ComputeChanges(unittest.TestCase):
    current = {
        "id": 10,
        "name": "sw1",
        "site": {"id": 1, "slug": "nyc", "name": "New York"},
        "status": {"value": "active", "label": "Active"},
        "tags": [{"id": 1, "slug": "core"}],
        "custom_fields": {"owner": "netops", "tier": 1},
        "serial": "",
    }

    def test_no_changes_when_desired_matches(self):
        desired = {"name": "sw1", "site": 1, "status": "active", "tags": ["core"], "custom_fields": {"tier": 1}}
        self.assertEqual(diff.compute_changes(desired, self.current), {})

    def test_reports_from_in_writable_form(self):
        changes = diff.compute_changes({"site": 2, "status": "planned"}, self.current)
        self.assertEqual(changes, {"site": {"from": 1, "to": 2}, "status": {"from": "active", "to": "planned"}})

    def test_custom_fields_partial(self):
        changes = diff.compute_changes({"custom_fields": {"tier": 2, "owner": "netops"}}, self.current)
        self.assertEqual(changes, {"custom_fields": {"from": {"tier": 1}, "to": {"tier": 2}}})

    def test_unknown_field_recorded_with_none_origin(self):
        changes = diff.compute_changes({"comments": "hi"}, self.current)
        self.assertEqual(changes, {"comments": {"from": None, "to": "hi"}})


class SnapshotToPayload(unittest.TestCase):
    def test_strips_read_only_and_converts(self):
        snap = {
            "id": 5,
            "url": "u",
            "display": "d",
            "created": "c",
            "last_updated": "l",
            "interface_count": 3,
            "name": "sw",
            "site": {"id": 1},
            "status": {"value": "active", "label": "Active"},
            "tags": [],
        }
        self.assertEqual(diff.snapshot_to_payload(snap), {"name": "sw", "site": 1, "status": "active", "tags": []})
        self.assertEqual(diff.snapshot_to_payload(snap, drop=["tags"]), {"name": "sw", "site": 1, "status": "active"})


class Rendering(unittest.TestCase):
    def test_render_changes_and_labels(self):
        lines = diff.render_changes({"status": {"from": "active", "to": "planned"}, "site": {"from": 1, "to": 2}})
        self.assertEqual(lines, ["site: 1 -> 2", "status: active -> planned"])
        self.assertEqual(diff.object_label({"display": "sw1"}), "sw1")
        self.assertEqual(diff.object_label({"address": "10.0.0.1/24"}), "10.0.0.1/24")
        self.assertEqual(diff.object_label({"id": 4}), "#4")
        self.assertEqual(diff.format_value("x" * 100, limit=10), "xxxxxxxxx…")


if __name__ == "__main__":
    unittest.main()
