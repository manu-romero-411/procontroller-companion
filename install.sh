#!/usr/bin/env bash
# Installer/uninstaller for procontroller-companion (Debian), defaulting to /usr/local
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-install}"

PREFIX=/usr/local
BIN_PATH="$PREFIX/bin/procontroller-companion"
UNIT_PATH="$PREFIX/lib/systemd/system/procontroller-companion.service"
CONF_PATH="$PREFIX/etc/procontroller_companion.ini"

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "This script must be run as root (sudo ./install.sh ...)" >&2
        exit 1
    fi
}

install_daemon() {
    require_root

    echo "[1/6] Installing dependencies..."
    apt-get update
    apt-get install -y python3-evdev python3-pyudev

    echo "[2/6] Loading the uinput kernel module..."
    modprobe uinput
    echo "uinput" > /etc/modules-load.d/uinput.conf

    echo "[3/6] Installing the daemon script to $BIN_PATH..."
    install -Dm755 "$SCRIPT_DIR/procontroller-companion.py" "$BIN_PATH"

    echo "[4/6] Installing the systemd service to $UNIT_PATH..."
    install -Dm644 "$SCRIPT_DIR/procontroller-companion.service" "$UNIT_PATH"

    echo "[5/6] Installing the config file (kept as-is if it already exists)..."
    if [[ -f "$CONF_PATH" ]]; then
        echo "    $CONF_PATH already exists, leaving it untouched."
    else
        install -Dm644 "$SCRIPT_DIR/procontroller_companion.ini" "$CONF_PATH"
    fi

    echo "[6/6] Enabling and starting the service..."
    systemctl daemon-reload
    systemctl enable --now procontroller-companion.service

    echo "Done. Check status with: systemctl status procontroller-companion.service"
    echo "Live logs: journalctl -u procontroller-companion.service -f"
    echo "Edit $CONF_PATH and restart the service to change the Home double-press command."
}

uninstall_daemon() {
    require_root
    local purge="${1:-}"

    echo "[1/3] Stopping and disabling the service..."
    systemctl disable --now procontroller-companion.service 2>/dev/null || true

    echo "[2/3] Removing installed files..."
    rm -f "$UNIT_PATH" "$BIN_PATH"
    systemctl daemon-reload

    if [[ "$purge" == "purge" ]]; then
        rm -f "$CONF_PATH"
        echo "[3/3] Removed config file $CONF_PATH."
    else
        echo "[3/3] Config file kept at $CONF_PATH. Run '$0 uninstall purge' to remove it too."
    fi

    echo "Done. (The uinput kernel module and /etc/modules-load.d/uinput.conf are left in place,"
    echo "since other software may rely on uinput too.)"
}

case "$ACTION" in
    install)
        install_daemon
        ;;
    uninstall)
        uninstall_daemon "${2:-}"
        ;;
    *)
        echo "Usage: $0 [install|uninstall [purge]]" >&2
        exit 1
        ;;
esac
