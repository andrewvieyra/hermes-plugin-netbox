import unittest

from .fake_netbox import FakeNetBox, seeded
from .helpers import submodule

client_mod = submodule("client")


class AuthAndConfig(unittest.TestCase):
    def test_auth_header_styles(self):
        self.assertEqual(client_mod.auth_header("nbt_key.secret"), "Bearer nbt_key.secret")
        self.assertEqual(client_mod.auth_header("0123456789abcdef"), "Token 0123456789abcdef")
        self.assertEqual(client_mod.auth_header("Token abc"), "Token abc")

    def test_settings_from_env(self):
        env = {
            "NETBOX_URL": "https://nb.example.com/",
            "NETBOX_TOKEN": "t",
            "NETBOX_VERIFY_SSL": "false",
            "NETBOX_TIMEOUT": "5",
        }
        s = client_mod.settings_from_env(env)
        self.assertEqual(s, {"url": "https://nb.example.com", "token": "t", "verify_ssl": False, "timeout": 5.0})
        with self.assertRaises(client_mod.ConfigError):
            client_mod.settings_from_env({"NETBOX_URL": "https://x"})
        with self.assertRaises(client_mod.ConfigError):
            client_mod.settings_from_env({"NETBOX_URL": "x", "NETBOX_TOKEN": "t"})
        self.assertFalse(client_mod.is_configured({}))
        self.assertTrue(client_mod.is_configured({"NETBOX_URL": "http://x", "NETBOX_TOKEN": "t"}))

    def test_validate_endpoint(self):
        self.assertEqual(client_mod.validate_endpoint("/api/dcim/devices/"), "dcim/devices")
        self.assertEqual(client_mod.validate_endpoint("plugins/bgp/sessions"), "plugins/bgp/sessions")
        for bad in ("devices", "dcim/devices/1", "../x", "DCIM/Devices", 5):
            with self.assertRaises(client_mod.NetBoxError):
                client_mod.validate_endpoint(bad)


class Requests(unittest.TestCase):
    def setUp(self):
        self.nb = seeded()
        self.client = client_mod.NetBoxClient(self.nb.base_url, "nbt_a.b", session=self.nb)

    def test_status_and_auth_header_sent(self):
        self.assertEqual(self.client.status()["netbox-version"], "4.3.0")
        bad = client_mod.NetBoxClient(self.nb.base_url, "", session=self.nb)
        with self.assertRaises(client_mod.NetBoxError) as ctx:
            bad.status()
        self.assertEqual(ctx.exception.status, 403)

    def test_get_object_404_is_none(self):
        self.assertIsNone(self.client.get_object("dcim/devices", 999))
        self.assertEqual(self.client.get_object("dcim/devices", 10)["name"], "sw1")

    def test_list_paginates_and_caps(self):
        nb = FakeNetBox()
        for i in range(1, 8):
            nb.seed("dcim/sites", {"name": f"s{i}", "slug": f"s{i}"})
        c = client_mod.NetBoxClient(nb.base_url, "nbt_a.b", session=nb)
        total, rows = c.list("dcim/sites", max_results=5, page_size=2)
        self.assertEqual(total, 7)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual(len([x for x in nb.calls if x["method"] == "GET"]), 3)

    def test_find_counts(self):
        self.assertEqual(self.client.find("dcim/devices", {"name": "sw1"})[0], 1)
        count, obj = self.client.find("dcim/devices", {"site": "nyc"})
        self.assertEqual((count, obj), (2, None))
        self.assertEqual(self.client.find("dcim/devices", {"name": "nope"}), (0, None))
        with self.assertRaises(client_mod.NetBoxError):
            self.client.find("dcim/devices", {})

    def test_error_carries_field_errors(self):
        with self.assertRaises(client_mod.NetBoxError) as ctx:
            self.client.create("dcim/devices", {"name": "x", "status": "bogus"})
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(list(ctx.exception.field_errors()), ["status"])
        self.assertIn("status", ctx.exception.to_dict()["body"])

    def test_transport_error_wrapped(self):
        class Boom:
            def request(self, *a, **k):
                raise ConnectionError("refused")

        c = client_mod.NetBoxClient("https://x", "nbt_a.b", session=Boom())
        with self.assertRaises(client_mod.NetBoxError) as ctx:
            c.status()
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("refused", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
