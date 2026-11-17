import copy
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from tendvpn.api import ApiError, provision
from tendvpn.cli import domain
from tendvpn.profile import FINAL_MASK, TAG
from tendvpn.render import compose, render
from tendvpn.storage import load

STATE = dict(node_domain="node.example.com", panel_domain="panel.example.com",
             sub_domain="sub.example.com", db_password="dbsecret", app_secret="appsecret",
             metrics_password="metricssecret")


class Panel:
    """Contract-shaped panel simulator, including lost POST responses."""
    def __init__(self):
        self.objects = {"config-profiles": [], "nodes": [], "hosts": [],
                        "internal-squads": [{"uuid": str(uuid4()), "name": "Default", "inbounds": []}]}
        self.users = []
        self.writes = []
        self.crash_after_post = None

    def call(self, method, path, body=None):
        if path == "/api/system/tools/x25519/generate":
            return {"keypairs": [{"privateKey": "TEST_PRIVATE_KEY"}]}
        if path == "/api/users/by-username/MAIN_USER":
            if not self.users:
                raise ApiError(method, path, 404)
            return copy.deepcopy(self.users[0])
        resource = path.split("/")[2]
        if method == "GET":
            items = copy.deepcopy(self.objects[resource])
            return {"configProfiles": items} if resource == "config-profiles" else (
                {"internalSquads": items} if resource == "internal-squads" else items)
        self.writes.append((method, path, copy.deepcopy(body)))
        if method == "PATCH":
            item = next(x for x in self.objects[resource] if x["uuid"] == body["uuid"])
            item.update(body)
            item["inbounds"] = [{"uuid": value} for value in body["inbounds"]]
            return copy.deepcopy(item)
        item = dict(copy.deepcopy(body), uuid=str(uuid4()))
        if resource == "config-profiles":
            item["inbounds"] = [{"uuid": str(uuid4()), "tag": TAG}]
        if resource == "users":
            item.pop("uuid")
            item["id"] = len(self.users) + 1
            item["activeInternalSquads"] = [{"uuid": value} for value in item["activeInternalSquads"]]
            item["subscriptionUrl"] = "https://sub.example.com/secret-short-uuid"
            self.users.append(item)
        else:
            self.objects[resource].append(item)
        if self.crash_after_post == resource:
            self.crash_after_post = None
            raise RuntimeError("Simulated lost response after committed POST")
        return copy.deepcopy(item)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.state = copy.deepcopy(STATE)
        self.panel = Panel()

    def test_requested_graph(self):
        user = provision(self.panel, self.state, self.path)
        profile = self.panel.objects["config-profiles"][0]
        inbound = profile["config"]["inbounds"][0]
        reality = inbound["streamSettings"]["realitySettings"]
        self.assertEqual(inbound["port"], 2053)
        self.assertEqual(reality["target"], "/dev/shm/nginx.sock")
        self.assertEqual(reality["serverNames"], ["node.example.com"])
        self.assertRegex(reality["shortIds"][0], r"^[0-9a-f]{16}$")
        node = self.panel.objects["nodes"][0]
        host = self.panel.objects["hosts"][0]
        self.assertEqual(node["configProfile"]["activeConfigProfileUuid"], profile["uuid"])
        self.assertEqual(host["nodes"], [node["uuid"]])
        self.assertEqual(host["remark"], "VPN - 1")
        self.assertEqual(host["finalMask"], FINAL_MASK)
        self.assertFalse(host["isHidden"])
        self.assertFalse(host["isDisabled"])
        self.assertEqual(user["username"], "MAIN_USER")
        self.assertEqual(user["expireAt"], "2099-12-31T23:59:59.000Z")
        self.assertEqual(user["hwidDeviceLimit"], 0)
        self.assertEqual(user["trafficLimitBytes"], 0)
        self.assertEqual(user["activeInternalSquads"][0]["uuid"], self.state["squad_uuid"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_repeat_keeps_keys_and_objects(self):
        provision(self.panel, self.state, self.path)
        original = copy.deepcopy(self.state)
        count = len(self.panel.writes)
        provision(self.panel, load(self.path), self.path)
        self.assertEqual(load(self.path), original)
        self.assertEqual(len(self.panel.writes), count)

    def test_resume_after_lost_post_response(self):
        for resource in ("config-profiles", "nodes", "hosts", "users"):
            with self.subTest(resource=resource):
                panel = Panel()
                state = copy.deepcopy(STATE)
                panel.crash_after_post = resource
                with self.assertRaisesRegex(RuntimeError, "Simulated"):
                    provision(panel, state, self.path)
                provision(panel, load(self.path), self.path)
                self.assertTrue(all(len(items) == 1 for items in panel.objects.values()))
                self.assertEqual(len(panel.users), 1)

    def test_preserves_existing_squad_inbounds(self):
        other = str(uuid4())
        self.panel.objects["internal-squads"][0]["inbounds"] = [{"uuid": other}]
        provision(self.panel, self.state, self.path)
        ids = [x["uuid"] for x in self.panel.objects["internal-squads"][0]["inbounds"]]
        self.assertIn(other, ids)
        self.assertEqual(len(ids), 2)

    def test_does_not_recreate_deleted_tracked_node(self):
        provision(self.panel, self.state, self.path)
        self.panel.objects["nodes"].clear()
        with self.assertRaisesRegex(RuntimeError, "удалён"):
            provision(self.panel, self.state, self.path)

    def test_does_not_overwrite_changed_keys(self):
        provision(self.panel, self.state, self.path)
        self.panel.objects["config-profiles"][0]["config"]["inbounds"][0]["port"] = 443
        with self.assertRaisesRegex(RuntimeError, "Профиль изменён"):
            provision(self.panel, self.state, self.path)

    def test_does_not_reset_changed_user_limit(self):
        provision(self.panel, self.state, self.path)
        self.panel.users[0]["hwidDeviceLimit"] = 2
        with self.assertRaisesRegex(RuntimeError, "MAIN_USER"):
            provision(self.panel, self.state, self.path)

    def test_api_server_failure_does_not_create_user(self):
        call = self.panel.call
        def fail(method, path, body=None):
            if path == "/api/users/by-username/MAIN_USER":
                raise ApiError(method, path, 500)
            return call(method, path, body)
        self.panel.call = fail
        with self.assertRaises(ApiError):
            provision(self.panel, self.state, self.path)
        self.assertEqual(self.panel.users, [])

    def test_no_public_management_ports(self):
        services = compose(STATE)["services"]
        for name in ("db", "redis", "subscription"):
            self.assertNotIn("ports", services[name])
        self.assertEqual(services["panel"]["ports"], ["127.0.0.1:3000:3000"])
        self.assertEqual(services["node"]["ports"], ["2053:2053/tcp"])
        self.assertTrue(all(":latest" not in x["image"] for x in services.values()))

    def test_socket_and_certificate_wiring(self):
        root = Path(self.temp.name)
        render(root, STATE)
        config = (root / "nginx.conf").read_text()
        self.assertIn("unix:/dev/shm/nginx.sock ssl proxy_protocol", config)
        self.assertIn("listen 443 ssl", config)
        self.assertIn("/.well-known/acme-challenge/", config)
        self.assertIn("proxy_pass http://panel:3000", config)
        for filename in ("db.env", "panel.env", "node.env", "subscription.env"):
            self.assertEqual((root / filename).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("dbsecret", (root / "compose.json").read_text())
        for name in ("node", "nginx"):
            self.assertIn("./socket:/dev/shm", compose(STATE)["services"][name]["volumes"])

    def test_domain_injection_rejected(self):
        for value in ("https://a.com", "a.com;}", "a.com/path", "*.a.com", "a..com",
                      "a.com\nlisten 9999", "-abc.com", "abc-.com", "203.0.113.10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                domain(value)
        self.assertEqual(domain("Node.Example.COM."), "node.example.com")

class SubscriptionTests(unittest.TestCase):
    def test_html_and_wrong_host_rejected(self):
        from tendvpn.subscription import validate_subscription
        for data in (b"<html>Welcome</html>", b"vless://id@wrong.example.com:2053?security=reality"):
            with self.assertRaises(RuntimeError):
                validate_subscription(data, "node.example.com")

    def test_raw_and_base64_with_finalmask(self):
        import base64
        import json
        from urllib.parse import urlencode
        from tendvpn.subscription import validate_subscription
        raw = ("vless://id@node.example.com:2053?" + urlencode({"fm": json.dumps(FINAL_MASK)})).encode()
        validate_subscription(raw, "node.example.com")
        validate_subscription(base64.b64encode(raw), "node.example.com")

    def test_xray_json_with_finalmask(self):
        import json
        from tendvpn.subscription import validate_subscription
        raw = {"outbounds": [{"protocol": "vless", "settings": {
            "vnext": [{"address": "node.example.com", "port": 2053}]},
            "streamSettings": {"finalmask": FINAL_MASK}}]}
        validate_subscription(json.dumps(raw).encode(), "node.example.com")

class ApiStartupTests(unittest.TestCase):
    def test_connection_reset_becomes_retryable_error(self):
        from tendvpn.api import Api
        from unittest.mock import Mock
        api = Api()
        api.opener = Mock()
        api.opener.open.side_effect = ConnectionResetError(104, 'Connection reset by peer')
        with self.assertRaisesRegex(RuntimeError, 'недоступна'):
            api.call('GET', '/api/auth/status')

    def test_register_password_satisfies_real_panel_contract(self):
        from tendvpn.api import Api
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            state = dict(admin_username='admin', admin_password='a1' * 20)
            api = Api()
            api.call = Mock(side_effect=[{'isRegisterAllowed': True},
                                        {'accessToken': 'session'}, {'token': 'service'}])
            api.authenticate(state, Path(temp) / 'state.json')
            password = api.call.call_args_list[1].args[2]['password']
            self.assertRegex(password, r'^(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9]).{24,}$')
            self.assertEqual(api.call.call_args_list[1].args[1], '/api/auth/register')
            self.assertEqual(load(Path(temp) / 'state.json')['admin_password'], password)
