"""The quick-install preset requested by TendVPN."""
import secrets

TAG = "VPN_VLESS_TCP_REALITY"
FINAL_MASK = {"tcp": [{"type": "fragment", "settings": {
    "length": "10-20", "packets": "tlshello", "interval": "10-20"}}]}


def make_profile(domain, private_key, short_id=None):
    return {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "tag": TAG, "port": 2053, "listen": "0.0.0.0", "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            "streamSettings": {
                "network": "raw", "security": "reality",
                "realitySettings": {
                    "xver": 1, "target": "/dev/shm/nginx.sock",
                    "shortIds": [short_id or secrets.token_hex(8)],
                    "privateKey": private_key, "fingerprint": "firefox",
                    "serverNames": [domain],
                },
            },
        }],
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"},
                      {"tag": "BLOCK", "protocol": "blackhole"}],
        "routing": {
            "rules": [
                {"port": "25,587,465,143,993,110,995", "type": "field", "outboundTag": "BLOCK"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "BLOCK"},
                {"ip": ["geoip:private"], "type": "field", "outboundTag": "BLOCK"},
                {"type": "field", "inboundTag": [TAG], "outboundTag": "DIRECT"},
            ], "domainStrategy": "AsIs",
        },
    }
