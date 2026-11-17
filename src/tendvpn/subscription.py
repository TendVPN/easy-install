"""Check that public subscription contains the requested host, not a HTML error page."""
import base64
import json
from urllib.parse import parse_qs, urlsplit
from .profile import FINAL_MASK


def validate_subscription(content, domain):
    text = content.decode("utf-8").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        def walk(value):
            if isinstance(value, list):
                return any(walk(x) for x in value)
            if not isinstance(value, dict):
                return False
            if value.get("protocol") == "vless":
                servers = value.get("settings", {}).get("vnext", [])
                stream = value.get("streamSettings", {})
                if (any(x.get("address") == domain and x.get("port") == 2053 for x in servers)
                        and stream.get("finalmask") == FINAL_MASK):
                    return True
            return any(walk(x) for x in value.values())
        if walk(data):
            return
    if "vless://" not in text and not text.startswith("<"):
        try:
            text = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_").decode()
        except (ValueError, UnicodeError):
            pass
    for line in text.splitlines():
        try:
            url = urlsplit(line.strip())
            if url.scheme != "vless" or url.hostname != domain or url.port != 2053:
                continue
            query = parse_qs(url.query)
            if json.loads(query.get("fm", ["null"])[0]) == FINAL_MASK:
                return
        except (ValueError, TypeError):
            continue
    raise RuntimeError("В публичной подписке нет VLESS-хоста на 2053 с заданным Final mask.")
