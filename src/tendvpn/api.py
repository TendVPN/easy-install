"""Local-only API client. Secrets are never passed in process arguments."""
import json
import secrets
import time
import urllib.request
import urllib.error
from .profile import TAG, FINAL_MASK, make_profile
from .storage import save


class ApiError(RuntimeError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f"Remnawave: {method} {path}: HTTP {status}. Ответ с секретами скрыт.")


class Api:
    def __init__(self):
        self.base = "http://127.0.0.1:3000"
        self.token = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None):
        headers = {"Content-Type": "application/json", "X-Forwarded-Proto": "https",
                   "X-Forwarded-For": "127.0.0.1", "X-Remnawave-Client-Type": "browser"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ApiError(method, path, exc.code) from None
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Remnawave недоступна или вернула неверный JSON: {method} {path}") from exc
        if "response" not in result:
            raise RuntimeError(f"Неожиданный формат API: {method} {path}")
        return result["response"]

    def wait(self, seconds=180):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.call("GET", "/api/auth/status")
                return
            except RuntimeError:
                time.sleep(3)
        raise RuntimeError("Панель не запустилась за 180 секунд. Откройте диагностику.")

    def authenticate(self, state, state_path):
        credentials = {"username": state["admin_username"], "password": state["admin_password"]}
        status = self.call("GET", "/api/auth/status")
        if status["isRegisterAllowed"] and not (
                len(state["admin_password"]) >= 24
                and any(x.isupper() for x in state["admin_password"])
                and any(x.islower() for x in state["admin_password"])
                and any(x.isdigit() for x in state["admin_password"])):
            state["admin_password"] = secrets.token_urlsafe(30) + "Aa1"
            credentials["password"] = state["admin_password"]
            save(state_path, state)
        action = "register" if status["isRegisterAllowed"] else "login"
        result = self.call("POST", "/api/auth/" + action, credentials)
        self.token = result["accessToken"]
        if not state.get("subscription_token"):
            result = self.call("POST", "/api/tokens", {
                "name": "tendvpn-subscription", "expiresInDays": 36500,
                "scopes": ["*"],
            })
            state["subscription_token"] = result["token"]
            save(state_path, state)


def provision(api, state, state_path):
    """Reconcile by durable UUID, recover POST success even if local save was interrupted.

    Names are reserved for this installer on its fresh panel. After the first lookup,
    only recorded UUIDs are used; unrelated panel objects are never pruned.
    """
    def remember(key, value):
        state[key] = value
        save(state_path, state)
        return value

    def ensure(key, items, match, path, body):
        existing = next((x for x in items if x["uuid"] == state.get(key)), None)
        if state.get(key) and existing is None:
            raise RuntimeError(f"Объект {key} удалён из панели. Автовосстановление остановлено.")
        if existing is None:
            matches = [x for x in items if match(x)]
            if len(matches) > 1:
                raise RuntimeError(f"Неоднозначный объект {key}; проверьте панель.")
            existing = matches[0] if matches else None
        if existing is None:
            existing = api.call("POST", path, body)
        remember(key, existing["uuid"])
        return existing

    profiles = api.call("GET", "/api/config-profiles")["configProfiles"]
    if not state.get("profile_config"):
        pairs = api.call("GET", "/api/system/tools/x25519/generate")["keypairs"]
        remember("profile_config", make_profile(state["node_domain"], pairs[0]["privateKey"]))
    profile = ensure("profile_uuid", profiles, lambda x: x["name"] == "TendVPN",
                     "/api/config-profiles", {"name": "TendVPN", "config": state["profile_config"]})
    # Reuse actual keys from an existing object after a crash between POST and save.
    inbound = next((x for x in profile["inbounds"] if x["tag"] == TAG), None)
    if inbound is None:
        raise RuntimeError("В профиле TendVPN отсутствует VPN_VLESS_TCP_REALITY.")
    actual = profile["config"]
    if actual != state["profile_config"]:
        # Refuse to silently override keys/config that an operator changed.
        if state.get("provisioned"):
            raise RuntimeError("Профиль изменён в панели. Автоматическая перезапись отключена.")
        remember("profile_config", actual)
    inbound_id = inbound["uuid"]
    node = ensure("node_uuid", api.call("GET", "/api/nodes"),
                  lambda x: x["name"] == "VPN_NODE_1", "/api/nodes", {
                      "name": "VPN_NODE_1", "address": "node", "port": 2222,
                      "configProfile": {"activeConfigProfileUuid": profile["uuid"],
                                        "activeInbounds": [inbound_id]},
                  })
    expected_profile = node["configProfile"]
    if expected_profile["activeConfigProfileUuid"] != profile["uuid"] or inbound_id not in {
            x["uuid"] if isinstance(x, dict) else x for x in expected_profile["activeInbounds"]}:
        raise RuntimeError("Привязка ноды изменена. Проверьте её в панели.")

    squads = api.call("GET", "/api/internal-squads")["internalSquads"]
    squad = ensure("squad_uuid", squads, lambda x: x["name"] == "Default",
                   "/api/internal-squads", {"name": "Default", "inbounds": [inbound_id]})
    inbound_ids = [x["uuid"] if isinstance(x, dict) else x for x in squad.get("inbounds", [])]
    if inbound_id not in inbound_ids:
        api.call("PATCH", "/api/internal-squads", {"uuid": squad["uuid"],
                                                  "inbounds": inbound_ids + [inbound_id]})
    host = ensure("host_uuid", api.call("GET", "/api/hosts"),
                  lambda x: x["remark"] == "VPN - 1", "/api/hosts", {
                      "remark": "VPN - 1", "address": state["node_domain"], "port": 2053,
                      "inbound": {"configProfileUuid": profile["uuid"],
                                  "configProfileInboundUuid": inbound_id},
                      "isDisabled": False, "isHidden": False, "fingerprint": "firefox",
                      "sni": state["node_domain"], "securityLayer": "DEFAULT",
                      "nodes": [node["uuid"]], "finalMask": FINAL_MASK,
                  })
    if (host["port"] != 2053 or host["address"] != state["node_domain"]
            or host.get("isDisabled") or host.get("isHidden")
            or host.get("finalMask") != FINAL_MASK
            or host["inbound"]["configProfileInboundUuid"] != inbound_id):
        raise RuntimeError("Параметры VPN - 1 отличаются от быстрого пресета. Проверьте панель.")
    try:
        user = api.call("GET", "/api/users/by-username/MAIN_USER")
    except ApiError as exc:
        if exc.status != 404:
            raise
        user = api.call("POST", "/api/users", {
            "username": "MAIN_USER", "status": "ACTIVE", "trafficLimitBytes": 0,
            "trafficLimitStrategy": "NO_RESET", "expireAt": "2099-12-31T23:59:59.000Z",
            "hwidDeviceLimit": 0, "activeInternalSquads": [squad["uuid"]],
        })
    if state.get("user_id") is not None and state["user_id"] != user["id"]:
        raise RuntimeError("MAIN_USER был заменён другим пользователем.")
    remember("user_id", user["id"])
    if (user["trafficLimitBytes"] != 0 or user["hwidDeviceLimit"] != 0
            or not user["expireAt"].startswith("2099-12-31") or user["status"] != "ACTIVE"
            or squad["uuid"] not in [x["uuid"] for x in user["activeInternalSquads"]]):
        raise RuntimeError("Настройки MAIN_USER отличаются от пресета. Автоперезапись отключена.")
    remember("subscription_url", user["subscriptionUrl"])
    remember("provisioned", True)
    return user
