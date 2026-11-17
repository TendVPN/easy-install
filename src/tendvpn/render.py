"""Generate only files owned by this installation."""
import json
from pathlib import Path
from .storage import save

VERSIONS = {
    "panel": "remnawave/backend:3.4.11",
    "node": "remnawave/node:3.4.6",
    "subscription": "remnawave/subscription-page:8.0.1",
    "nginx": "nginx:1.30", "db": "postgres:18.6",
    "redis": "valkey/valkey:9.1.2-alpine",
}


def compose(state):
    def service(image, **kwargs):
        return {"image": image, "restart": "unless-stopped", "logging": {
            "driver": "local", "options": {"max-size": "10m", "max-file": "3"}}, **kwargs}
    services = {
        "db": service(VERSIONS["db"], env_file=["db.env"],
                      volumes=["database:/var/lib/postgresql"], shm_size="256mb",
                      healthcheck={"test": ["CMD-SHELL", "pg_isready -U postgres -d postgres"],
                                   "interval": "3s", "timeout": "5s", "retries": 30}),
        "redis": service(VERSIONS["redis"], volumes=["redis-socket:/var/run/valkey"],
                         command=["valkey-server", "--save", "", "--appendonly", "no",
                                  "--maxmemory-policy", "noeviction", "--unixsocket",
                                  "/var/run/valkey/valkey.sock", "--unixsocketperm", "777", "--port", "0"],
                         healthcheck={"test": ["CMD", "valkey-cli", "-s",
                                               "/var/run/valkey/valkey.sock", "ping"],
                                      "interval": "3s", "timeout": "5s", "retries": 30}),
        "panel": service(VERSIONS["panel"], env_file=["panel.env"],
                         ports=["127.0.0.1:3000:3000"], volumes=["redis-socket:/var/run/valkey"],
                         depends_on={"db": {"condition": "service_healthy"},
                                     "redis": {"condition": "service_healthy"}},
                         healthcheck={"test": ["CMD", "curl", "-f", "http://localhost:3001/health"],
                                      "interval": "10s", "timeout": "5s", "retries": 30,
                                      "start_period": "30s"}),
        "node": service(VERSIONS["node"], env_file=["node.env"],
                        ports=["2053:2053/tcp"], volumes=["./socket:/dev/shm"],
                        ulimits={"nofile": {"soft": 1048576, "hard": 1048576}}),
        "subscription": service(VERSIONS["subscription"], env_file=["subscription.env"],
                                depends_on={"panel": {"condition": "service_healthy"}}),
        "nginx": service(VERSIONS["nginx"], ports=["80:80", "443:443"],
                         volumes=["./nginx.conf:/etc/nginx/conf.d/default.conf:ro",
                                  "./socket:/dev/shm", "./acme:/var/www/acme:ro",
                                  "./site:/var/www/site:ro", "/etc/letsencrypt:/etc/letsencrypt:ro"],
                         command=["sh", "-c", "rm -f /dev/shm/nginx.sock && exec nginx -g 'daemon off;'"],
                         depends_on={"panel": {"condition": "service_healthy"},
                                     "subscription": {"condition": "service_started"}}),
    }
    return {"name": "tendvpn", "services": services,
            "volumes": {"database": {}, "redis-socket": {}}}


def nginx(state):
    panel, sub, node = (state[k] for k in ("panel_domain", "sub_domain", "node_domain"))
    cert = "/etc/letsencrypt/live/tendvpn"
    result = f'''
map $http_upgrade $connection_upgrade {{ default upgrade; "" close; }}
map $proxy_protocol_addr $client_ip {{ "" $remote_addr; default $proxy_protocol_addr; }}
server_tokens off;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_certificate {cert}/fullchain.pem;
ssl_certificate_key {cert}/privkey.pem;
server {{
    listen 80 default_server;
    server_name _;
    location ^~ /.well-known/acme-challenge/ {{ root /var/www/acme; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
server {{
    listen 443 ssl default_server;
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol default_server;
    server_name {node};
    root /var/www/site;
    index index.html;
    location / {{ try_files $uri $uri/ =404; }}
}}
'''
    for domain, target in ((panel, "panel:3000"), (sub, "subscription:3010")):
        result += f'''
server {{
    listen 443 ssl;
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    server_name {domain};
    http2 on;
    location / {{
        proxy_pass http://{target};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header X-Real-IP $client_ip;
        proxy_set_header X-Forwarded-For $client_ip;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_read_timeout 60s;
    }}
}}
'''
    return result


def render(root, state):
    root = Path(root)
    for directory in ("socket", "acme", "site"):
        (root / directory).mkdir(exist_ok=True, mode=0o755)
    (root / "site/index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<title>Welcome</title><h1>Welcome</h1><p>This website is online.</p></html>\n')
    (root / "site/index.html").chmod(0o644)
    save(root / "compose.json", compose(state))
    (root / "nginx.conf").write_text(nginx(state))
    # Hex secrets generated internally; domains are validated before reaching here.
    envs = {
        "db.env": {"POSTGRES_USER": "postgres", "POSTGRES_DB": "postgres",
                   "POSTGRES_PASSWORD": state["db_password"]},
        "panel.env": {
            "APP_PORT": "3000", "METRICS_PORT": "3001", "API_INSTANCES": "1",
            "DATABASE_URL": f"postgresql://postgres:{state['db_password']}@db:5432/postgres",
            "REDIS_SOCKET": "/var/run/valkey/valkey.sock", "APP_SECRET": state["app_secret"],
            "PANEL_DOMAIN": state["panel_domain"], "FRONT_END_DOMAIN": state["panel_domain"],
            "SUB_PUBLIC_DOMAIN": state["sub_domain"], "IS_TELEGRAM_NOTIFICATIONS_ENABLED": "false",
            "METRICS_USER": "metrics", "METRICS_PASS": state["metrics_password"],
            "WEBHOOK_ENABLED": "false",
        },
        "node.env": {"NODE_PORT": "2222", "SECRET_KEY": state.get("node_secret", "pending")},
        "subscription.env": {"APP_PORT": "3010", "REMNAWAVE_PANEL_URL": "http://panel:3000",
                             "REMNAWAVE_API_TOKEN": state.get("subscription_token", "pending"),
                             "TRUST_PROXY": "1"},
    }
    for name, values in envs.items():
        path = root / name
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        path.chmod(0o600)
