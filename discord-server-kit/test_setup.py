import copy
from pathlib import Path
import tempfile
import unittest
import setup_server as s


class FakeDiscord:
    def __init__(self):
        self.roles = [{"id": "100", "name": "@everyone", "permissions": str(s.VIEW | s.SEND)},
                      {"id": "101", "name": "Provisioner", "permissions": str(s.REQUIRED)}]
        self.channels, self.messages, self.mutations = [], {}, 0
        self.serial = 200

    def request(self, method, route, data=None):
        if method == "GET":
            if route == "/users/@me":
                return {"id": "9", "bot": True}
            if route == "/guilds/100":
                return {"name": "Test"}
            if route == "/guilds/100/members/9":
                return {"roles": ["101"]}
            if route == "/guilds/100/roles":
                return copy.deepcopy(self.roles)
            if route == "/guilds/100/channels":
                return copy.deepcopy(self.channels)
            if route.endswith("/messages?limit=50"):
                return copy.deepcopy(self.messages.get(route.split("/")[2], []))
        if method == "POST":
            self.mutations += 1
            self.serial += 1
            obj = {**copy.deepcopy(data), "id": str(self.serial)}
            if route.endswith("/roles"):
                self.roles.append(obj)
            elif route == "/guilds/100/channels":
                self.channels.append(obj)
            elif route.endswith("/messages"):
                obj["author"] = {"id": "9"}
                self.messages.setdefault(route.split("/")[2], []).append(obj)
            else:
                raise AssertionError(route)
            return copy.deepcopy(obj)
        raise AssertionError((method, route))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.config = s.load_config(Path(__file__).with_name("server.json"))
        self.ids = {r["key"]: str(i + 500) for i, r in enumerate(self.config["roles"])}

    def test_private_finance_access(self):
        cat = next(c for c in self.config["categories"] if c["key"] == "ops")
        channel = next(c for c in cat["channels"] if c["key"] == "finance")
        acl = s.overwrites(cat, channel, "100", "9", self.ids)
        self.assertTrue(int(acl[0]["deny"]) & s.VIEW)
        allowed_roles = {a["id"] for a in acl if a["type"] == 0 and int(a["allow"]) & s.VIEW}
        self.assertEqual(allowed_roles, {self.ids["owner"], self.ids["operator"]})

    def test_readonly_overrides_inherited_send(self):
        cat = next(c for c in self.config["categories"] if c["key"] == "orders")
        channel = cat["channels"][0]
        acl = s.overwrites(cat, channel, "100", "9", self.ids)
        effective = s.VIEW | s.SEND
        effective = (effective & ~int(acl[0]["deny"])) | int(acl[0]["allow"])
        worker = next(a for a in acl if a["id"] == self.ids["worker"])
        effective = (effective & ~int(worker["deny"])) | int(worker["allow"])
        self.assertTrue(effective & s.VIEW)
        self.assertFalse(effective & s.SEND)

    def test_provision_and_repeat_without_duplicates(self):
        api = FakeDiscord()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = s.provision(api, self.config, "100", "Test", path)
            count = api.mutations
            second = s.provision(api, self.config, "100", "Test", path)
            self.assertEqual(count, api.mutations)
            self.assertEqual(first, second)
            self.assertEqual(len(api.roles), 2 + len(self.config["roles"]))
            self.assertEqual(len(api.channels), 24)

    def test_wrong_server_no_mutations(self):
        api = FakeDiscord()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(s.SetupError):
                s.provision(api, self.config, "100", "Wrong", Path(directory) / "state.json")
        self.assertEqual(api.mutations, 0)

    def test_name_collision_no_mutations(self):
        api = FakeDiscord()
        api.roles.append({"id": "777", "name": self.config["roles"][0]["name"], "permissions": "0"})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(s.SetupError):
                s.provision(api, self.config, "100", "Test", Path(directory) / "state.json")
        self.assertEqual(api.mutations, 0)


if __name__ == "__main__":
    unittest.main()
