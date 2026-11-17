#!/bin/sh
# POSIX bootstrap; installation logic is Python 3 stdlib.
set -eu
main() {
    [ "$(id -u)" = 0 ] || { echo 'Запустите от root на VPS Ubuntu 24.04.' >&2; exit 1; }
    command -v python3 >/dev/null || { echo 'Нужен Python 3: apt install python3' >&2; exit 1; }
    staging=''
    trap '[ -z "$staging" ] || rm -rf -- "$staging"' EXIT HUP INT TERM
    if [ "${1:-}" = '--local' ]; then
        source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
    else
        repo=${TENDVPN_REPO:-TendVPN/easy-install}
        ref=${TENDVPN_REF:-main}
        printf '%s' "$repo" | LC_ALL=C grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || exit 2
        printf '%s' "$ref" | LC_ALL=C grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]*$' || exit 2
        command -v curl >/dev/null || { echo 'Нужен curl: apt install curl' >&2; exit 1; }
        staging=$(mktemp -d)
        curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 \
            "https://codeload.github.com/$repo/tar.gz/$ref" -o "$staging/source.tgz"
        python3 - "$staging" <<'PY'
import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
with tarfile.open(root / 'source.tgz') as archive:
    archive.extractall(root / 'source', filter='data')
PY
        source_dir=$(find "$staging/source" -mindepth 1 -maxdepth 1 -type d)
    fi
    [ -f "$source_dir/src/tendvpn/cli.py" ] || { echo 'Неверный архив установщика.' >&2; exit 1; }
    install -d -m 755 /usr/local/lib/tendvpn
    # Deploy into a new directory, then atomically switch the symlink.
    target=$(mktemp -d /usr/local/lib/tendvpn/release.XXXXXXXX)
    cp -R "$source_dir/src" "$source_dir/tendvpn" "$target/"
    chmod -R a+rX "$target"
    chmod 755 "$target/tendvpn"
    ln -s "$target/tendvpn" /usr/local/bin/tendvpn.new
    mv -Tf /usr/local/bin/tendvpn.new /usr/local/bin/tendvpn
    echo 'Установщик сохранён. Для повторного запуска: tendvpn'
    # The Python menu must read the terminal, never the curl|sh source stream.
    if [ -r /dev/tty ]; then
        /usr/local/bin/tendvpn </dev/tty
    else
        echo 'Нет интерактивного терминала. Подключитесь по SSH и запустите tendvpn.' >&2
        exit 1
    fi
}
main "$@"
