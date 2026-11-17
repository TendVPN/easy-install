"""Interactive installer for a fresh Ubuntu 24.04 VPS."""
import argparse
import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from .api import Api, provision
from .render import render, VERSIONS
from .storage import load, save
from .subscription import validate_subscription

ROOT = Path("/opt/tendvpn")
STATE = ROOT / "state.json"


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def dc(*args, **kwargs):
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return run(["docker", "compose", "--project-directory", str(ROOT), "-f",
                str(ROOT / "compose.json"), *args], **kwargs)


def domain(value):
    value = value.strip().lower().rstrip(".")
    if (len(value) > 253 or len(value.split(".")) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                   for part in value.split("."))):
        raise ValueError("Введите домен без https://, пути и порта; кириллицу — в punycode.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("Нужен домен, а не IP-адрес.")


def ask_domain(prompt):
    while True:
        try:
            return domain(input(prompt + ": "))
        except ValueError as exc:
            print(exc)


def require_host():
    if os.geteuid() != 0:
        raise RuntimeError("Запускайте установщик на VPS от root (sudo).")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "24.04":
        raise RuntimeError("Первая версия поддерживает Ubuntu 24.04.")
    if os.uname().machine not in ("x86_64", "aarch64"):
        raise RuntimeError("Поддерживаются только amd64 и arm64.")


def check_ports():
    for port in (80, 443, 2053, 3000):
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                raise RuntimeError(f"Порт {port}/tcp занят. Нужен чистый VPS.") from None


def check_dns(state):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("https://api.ipify.org", timeout=15) as result:
            public = str(ipaddress.IPv4Address(result.read().decode().strip()))
    except Exception as exc:
        raise RuntimeError("Не удалось определить публичный IPv4 сервера.") from exc
    for key in ("panel_domain", "sub_domain", "node_domain"):
        name = state[key]
        try:
            resolved = {entry[4][0] for entry in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)}
        except socket.gaierror:
            raise RuntimeError(f"DNS ещё не настроен: {name}. Создайте A-запись → {public}.") from None
        # MVP is IPv4 only. Reject stale AAAA rather than getting an intermittent ACME failure.
        if resolved != {public}:
            raise RuntimeError(f"DNS {name}: {', '.join(sorted(resolved))}. Нужна только A-запись → {public}; "
                               "отключите проксирование Cloudflare и уберите AAAA для этой установки.")
    return public


def dependencies():
    if (all(shutil.which(name) for name in ("curl", "certbot", "docker"))
            and Path("/etc/ssl/certs/ca-certificates.crt").exists()
            and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0):
        run(["systemctl", "enable", "--now", "docker"])
        return
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    run(["apt-get", "update"], env=env)
    run(["apt-get", "-o", "DPkg::Lock::Timeout=600", "install", "-y", "ca-certificates", "curl", "certbot"], env=env)
    if not shutil.which("docker"):
        # Official Docker APT repository, not an opaque curl|sh installer.
        keydir = Path("/etc/apt/keyrings")
        keydir.mkdir(exist_ok=True, mode=0o755)
        run(["curl", "-fsSL", "--retry", "3", "https://download.docker.com/linux/ubuntu/gpg",
             "-o", str(keydir / "docker.asc")])
        (keydir / "docker.asc").chmod(0o644)
        arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
        Path("/etc/apt/sources.list.d/tendvpn-docker.sources").write_text(
            "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\n"
            f"Components: stable\nArchitectures: {arch}\nSigned-By: /etc/apt/keyrings/docker.asc\n")
        run(["apt-get", "update"], env=env)
        run(["apt-get", "-o", "DPkg::Lock::Timeout=600", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
             "docker-buildx-plugin", "docker-compose-plugin"], env=env)
    run(["docker", "compose", "version"])
    run(["systemctl", "enable", "--now", "docker"])


def credentials(state):
    print(f"\nПанель: https://{state['panel_domain']}")
    print(f"Логин: {state['admin_username']}\nПароль: {state['admin_password']}")
    if state.get("subscription_url"):
        print(f"\nПодписка MAIN_USER: {state['subscription_url']}")
        print(f"Добавить в Happ: happ://add/{state['subscription_url']}")
        alternate = state["subscription_url"].replace(
            "https://" + state["sub_domain"] + "/", "https://" + state["sub_domain"] + ":2053/", 1)
        print(f"Запасная подписка (HTTPS на 2053): {alternate}")
    print("\nДанные также доступны командой: tendvpn credentials")


def install_timer():
    Path("/etc/systemd/system/tendvpn-cert-renew.service").write_text('''[Unit]
Description=TendVPN certificate renewal
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --cert-name tendvpn --webroot -w /opt/tendvpn/acme --deploy-hook "/usr/bin/docker compose --project-directory /opt/tendvpn -f /opt/tendvpn/compose.json exec -T nginx nginx -s reload"
''')
    Path("/etc/systemd/system/tendvpn-cert-renew.timer").write_text('''[Unit]
Description=Renew TendVPN TLS certificates twice daily
[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=3600
Persistent=true
[Install]
WantedBy=timers.target
''')
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "tendvpn-cert-renew.timer"])


def verify(api, state):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        node = api.call("GET", "/api/nodes/" + state["node_uuid"])
        if node.get("isConnected") and node.get("xrayUptime", 0) > 0:
            break
        time.sleep(3)
    else:
        raise RuntimeError("Нода не запустила Xray за 120 секунд. Откройте диагностику.")
    # Resolve and validate public HTTPS, including the real subscription response.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in (f"https://{state['panel_domain']}",
                f"https://{state['node_domain']}", state["subscription_url"]):
        deadline = time.monotonic() + 60
        while True:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Happ/2.0", "Accept": "*/*"})
                with opener.open(req, timeout=15) as response:
                    content = response.read()
                    if not content:
                        raise RuntimeError("Пустой ответ HTTPS")
                    if url == state["subscription_url"]:
                        validate_subscription(content, state["node_domain"])
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Проверка публичного HTTPS/подписки не прошла. Проверьте DNS и порты.") from None
                time.sleep(3)


def quick_install():
    require_host()
    if STATE.exists():
        state = load(STATE)
        if state.get("ready"):
            print("Быстрая установка уже завершена. Настройки не перезаписаны.")
            credentials(state)
            return
        print("Продолжаю незавершённую установку с сохранёнными параметрами.")
    else:
        check_ports()
        if (ROOT / "compose.json").exists():
            raise RuntimeError("Есть compose.json без state.json. Сначала восстановите состояние.")
        if Path("/etc/letsencrypt/renewal/tendvpn.conf").exists():
            raise RuntimeError("Сертификат tendvpn уже существует вне этой установки.")
        print("Нужны три разных поддомена с A-записями на IPv4 этого VPS.")
        print("Откройте 80, 443 и 2053/tcp в firewall провайдера. SSH менять не будем.")
        state = {"panel_domain": ask_domain("Домен панели"),
                 "sub_domain": ask_domain("Домен страницы подписки"),
                 "node_domain": ask_domain("Домен ноды")}
        if len(set(state.values())) != 3:
            raise RuntimeError("Домены панели, подписки и ноды должны различаться.")
        state.update(admin_username="admin", admin_password=secrets.token_urlsafe(30) + "Aa1",
                     db_password=secrets.token_hex(32), app_secret=secrets.token_hex(64),
                     metrics_password=secrets.token_hex(24))
        ip = check_dns(state)
        print(f"\nУстановка: панель + нода + страница подписки на {ip}.")
        print("Будет создан MAIN_USER: до 31.12.2099, без лимита трафика и устройств.")
        print("Let's Encrypt: https://letsencrypt.org/repository/ — выпуск принимает условия CA.")
        if input("Установить? [да/нет]: ").strip().lower() not in ("да", "yes", "y"):
            return
        ROOT.mkdir(exist_ok=True, mode=0o700)
        ROOT.chmod(0o700)
        save(STATE, state)
    check_dns(state)
    dependencies()
    render(ROOT, state)
    dc("config", "--quiet")
    dc("pull")
    if not state.get("certificate_issued"):
        run(["certbot", "certonly", "--standalone", "--non-interactive", "--agree-tos",
             "--register-unsafely-without-email", "--cert-name", "tendvpn",
             "-d", state["panel_domain"], "-d", state["sub_domain"], "-d", state["node_domain"]])
        state["certificate_issued"] = True
        save(STATE, state)
    print("Запускаю панель и базу данных…")
    dc("up", "-d", "db", "redis", "panel")
    api = Api()
    api.wait()
    api.authenticate(state, STATE)
    if not state.get("node_secret"):
        state["node_secret"] = api.call("GET", "/api/keygen")["secretKey"]
        save(STATE, state)
    provision(api, state, STATE)
    render(ROOT, state)
    dc("up", "-d", "node", "subscription")
    dc("run", "--rm", "--no-deps", "-T", "--interactive=false", "nginx", "nginx", "-t")
    dc("up", "-d", "nginx")
    verify(api, state)
    if not state.get("renewal_configured"):
        run(["certbot", "reconfigure", "--non-interactive", "--cert-name", "tendvpn",
             "--webroot", "-w", str(ROOT / "acme"), "--deploy-hook",
             "/usr/bin/docker compose --project-directory /opt/tendvpn "
             "-f /opt/tendvpn/compose.json exec -T nginx nginx -s reload"])
        state["renewal_configured"] = True
        save(STATE, state)
    install_timer()
    state["ready"] = True
    save(STATE, state)
    print("\nУстановка завершена. Панель, Xray и публичные HTTPS-адреса проверены.")
    credentials(state)


def backup():
    state = load(STATE)
    name = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = ROOT / "backups" / name
    target.mkdir(parents=True, mode=0o700)
    with (target / "database.dump").open("wb") as stream:
        dc("exec", "-T", "db", "pg_dump", "-U", "postgres", "-d", "postgres", "-Fc", stdout=stream)
    # Config backup includes all keys needed to recover the DB and panel login.
    for filename in ("state.json", "compose.json", "panel.env", "db.env", "node.env",
                     "subscription.env", "nginx.conf"):
        shutil.copy2(ROOT / filename, target / filename)
    save(target / "manifest.json", {"createdAt": name, "versions": VERSIONS,
                                   "certificates": "Reissue on recovery; not included."})
    print(f"Резервная копия: {target} (содержит секреты)")
    return target


def renew():
    run(["certbot", "renew", "--cert-name", "tendvpn", "--webroot", "-w", str(ROOT / "acme"),
         "--deploy-hook", "/usr/bin/docker compose --project-directory /opt/tendvpn "
                          "-f /opt/tendvpn/compose.json exec -T nginx nginx -s reload"])


def update():
    state = load(STATE)
    if not state.get("ready"):
        raise RuntimeError("Сначала завершите быструю установку.")
    backup()
    # This MVP reapplies this release's pinned versions, never switches to :latest.
    render(ROOT, state)
    dc("pull")
    dc("up", "-d")
    api = Api()
    api.wait()
    api.authenticate(state, STATE)
    verify(api, state)
    print("Компоненты приведены к версиям текущего релиза и проверены.")


def remove():
    print("Будут остановлены и удалены контейнеры TendVPN. Данные и копии останутся в /opt/tendvpn.")
    if input("Введите УДАЛИТЬ для продолжения: ").strip() != "УДАЛИТЬ":
        return
    backup()
    run(["systemctl", "disable", "--now", "tendvpn-cert-renew.timer"])
    dc("down")
    state = load(STATE)
    state["ready"] = False
    save(STATE, state)
    print("Контейнеры удалены. База, настройки и сертификаты сохранены.")


def menu():
    while True:
        print("\nTendVPN Easy — предварительная версия\n"
              "1. Установить панель + ноду + страницу подписки\n"
              "2. Статус компонентов\n3. Показать панель и ссылку Happ\n"
              "4. Обновить компоненты до версий этого релиза\n"
              "5. Проверить / продлить сертификаты\n6. Резервная копия\n"
              "7. Диагностика\n8. Удалить контейнеры (сохранить данные)\n0. Выход")
        choice = input("> ").strip()
        actions = {"1": quick_install, "2": lambda: dc("ps"),
                   "3": lambda: credentials(load(STATE)), "4": update, "5": renew,
                   "6": backup, "7": diagnose, "8": remove}
        if choice == "0":
            return
        if choice not in actions:
            continue
        try:
            if choice != "1":
                require_host()
                if not STATE.exists():
                    raise RuntimeError("Сначала выполните установку.")
            actions[choice]()
        except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            print(f"Ошибка: {exc}\nПовторный запуск: tendvpn", file=sys.stderr)


def diagnose():
    dc("ps")
    dc("logs", "--tail", "50")
    print("Логи могут содержать секреты. Перед публикацией удалите токены, ключи и ссылки подписки.")


def main():
    parser = argparse.ArgumentParser(description="TendVPN Easy")
    parser.add_argument("command", nargs="?", default="menu",
                        choices=["menu", "install", "credentials", "status", "backup", "renew",
                                 "update", "remove", "diagnose", "plan"])
    parser.add_argument("--output", type=Path, help="Каталог для безопасного preview (только plan)")
    args = parser.parse_args()
    if args.command == "plan":
        preview = {"panel_domain": "panel.example.com", "sub_domain": "sub.example.com",
                   "node_domain": "node.example.com", "db_password": "PREVIEW_ONLY",
                   "app_secret": "PREVIEW_ONLY", "metrics_password": "PREVIEW_ONLY"}
        if not args.output:
            parser.error("plan требует --output DIR")
        args.output.mkdir(parents=True, exist_ok=True)
        render(args.output, preview)
        print(f"Preview записан в {args.output}. Компоненты не установлены.")
        return
    require_host()
    # One operation per host; separate from ROOT so the lock exists before install.
    with open("/run/lock/tendvpn.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Другой процесс TendVPN уже работает.") from None
        actions = {"menu": menu, "install": quick_install, "credentials": lambda: credentials(load(STATE)),
                   "status": lambda: dc("ps"), "backup": backup, "renew": renew,
                   "update": update, "remove": remove, "diagnose": diagnose}
        actions[args.command]()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nОстановлено. Сохранённую установку можно продолжить.")
        sys.exit(130)
