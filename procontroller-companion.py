#!/usr/bin/env python3
"""
procontroller-companion

Daemon for Nintendo Switch Pro Controllers connected to a Linux PC:

  1. Maps the capture button (BTN_Z) to Print Screen (KEY_SYSRQ).
  2. Runs a user-configured shell command when the Home button
     (BTN_MODE) is pressed twice in quick succession.

Works with any number of controllers, hotplugged at any time, regardless
of player number.

Dependencies (Debian):
    sudo apt install python3-evdev python3-pyudev

Config file (see procontroller_companion.ini), checked in this order:
    /etc/procontroller_companion.ini
    /usr/local/etc/procontroller_companion.ini

    [general]
    home_launch = <shell command>

The command is passed verbatim to `bash -c "<command>"`, so quoting,
spaces, pipes, redirects and environment variables all follow normal
bash rules.

Requires access to /dev/input/event* and /dev/uinput -> intended to run
as a systemd service (see procontroller-companion.service).
"""

import configparser
import os
import subprocess
import threading
import time
import evdev
import pyudev
from evdev import UInput, ecodes as e

TARGET_NAME_SUBSTR = "Pro Controller"
CAPTURE_CODE = e.BTN_Z      # capture button, mapped by the hid-nintendo driver
HOME_CODE = e.BTN_MODE      # home button (assumed mapping - verify with PROCONTROLLER_DEBUG=1)
DOUBLE_PRESS_WINDOW = 0.4   # seconds between presses to count as a double press

# Set PROCONTROLLER_DEBUG=1 in the environment (or in the systemd unit) to log
# every EV_KEY event with its symbolic name, e.g. to confirm which code the
# Home button actually sends on this controller/driver version.
DEBUG = bool(os.environ.get("PROCONTROLLER_DEBUG"))

CONFIG_PATHS = [
    "/etc/procontroller_companion.ini",
    "/usr/local/etc/procontroller_companion.ini",
]

active = {}
active_lock = threading.Lock()

# Single persistent virtual keyboard, shared by every controller
ui = UInput({e.EV_KEY: [e.KEY_SYSRQ]}, name="procontroller-companion-vkbd")


def load_home_command():
    cp = configparser.ConfigParser()
    for path in CONFIG_PATHS:
        if os.path.isfile(path):
            cp.read(path)
            return cp.get("general", "home_launch", fallback=None)
    return None


def get_active_graphical_session():
    """Return (username, uid) of the user with an active seat session,
    so home_launch can run in their session instead of root's."""
    try:
        out = subprocess.check_output(["loginctl", "list-sessions", "--no-legend"], text=True)
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        session_id = parts[0]
        try:
            props_raw = subprocess.check_output(
                ["loginctl", "show-session", session_id,
                 "-p", "Name", "-p", "User", "-p", "Seat", "-p", "Active"],
                text=True,
            )
        except Exception:
            continue
        props = dict(p.split("=", 1) for p in props_raw.strip().splitlines() if "=" in p)
        if props.get("Seat") and props.get("Active") == "yes":
            return props.get("Name"), props.get("User")
    return None


def find_user_display_env(uid):
    """Best-effort: look for DISPLAY/WAYLAND_DISPLAY/XAUTHORITY in a
    running process owned by uid, so GUI commands can reach the screen."""
    result = {}
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.stat(f"/proc/{pid}").st_uid != uid:
                    continue
                with open(f"/proc/{pid}/environ", "rb") as f:
                    data = f.read()
            except (OSError, PermissionError):
                continue
            env_vars = dict(
                item.split("=", 1)
                for item in data.decode(errors="ignore").split("\0")
                if "=" in item
            )
            if "DISPLAY" in env_vars or "WAYLAND_DISPLAY" in env_vars:
                for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
                    if key in env_vars:
                        result.setdefault(key, env_vars[key])
                break
    except Exception:
        pass
    return result


def trigger_home_action():
    cmd = load_home_command()
    if not cmd:
        print("[!] Home double-press: no home_launch command configured", flush=True)
        return

    session = get_active_graphical_session()
    if not session:
        print("[!] Home double-press: no active graphical session found, running without one", flush=True)
        subprocess.Popen(["/bin/bash", "-c", cmd])
        return

    username, uid = session
    env_pairs = {"XDG_RUNTIME_DIR": f"/run/user/{uid}"}
    bus_path = f"/run/user/{uid}/bus"
    if os.path.exists(bus_path):
        env_pairs["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    env_pairs.update(find_user_display_env(int(uid)))

    print(f"[+] Home double-press -> running as {username}: {cmd}", flush=True)
    # Fire-and-forget; runs as the logged-in user with their session bus
    # and display, not as root. Bash itself handles quoting/escaping.
    env_args = [f"{k}={v}" for k, v in env_pairs.items()]
    subprocess.Popen(["runuser", "-u", username, "--", "env", *env_args, "bash", "-c", cmd])


def handle_device(path):
    try:
        dev = evdev.InputDevice(path)
    except OSError:
        return

    if TARGET_NAME_SUBSTR not in dev.name:
        dev.close()
        return

    print(f"[+] Listening on '{dev.name}' at {path}", flush=True)
    last_home_press = 0.0
    try:
        for event in dev.read_loop():
            if event.type != e.EV_KEY:
                continue

            if DEBUG:
                name = e.keys.get(event.code, event.code)
                print(f"[debug] {dev.name}: code={event.code} name={name} value={event.value}", flush=True)

            if event.code == CAPTURE_CODE:
                ui.write(e.EV_KEY, e.KEY_SYSRQ, event.value)  # 1=down 0=up
                ui.syn()

            elif event.code == HOME_CODE and event.value == 1:
                now = time.monotonic()
                if now - last_home_press <= DOUBLE_PRESS_WINDOW:
                    last_home_press = 0.0
                    trigger_home_action()
                else:
                    last_home_press = now
    except OSError:
        print(f"[-] {path} disconnected", flush=True)
    finally:
        with active_lock:
            active.pop(path, None)


def spawn(path):
    with active_lock:
        if path in active:
            return
        t = threading.Thread(target=handle_device, args=(path,), daemon=True)
        active[path] = t
        t.start()


def scan_existing():
    for path in evdev.list_devices():
        spawn(path)


def monitor_udev():
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="input")
    print("[*] Watching for controller connect/disconnect events...", flush=True)
    for device in iter(monitor.poll, None):
        if device.action == "add" and device.device_node and "event" in device.device_node:
            time.sleep(0.3)  # let the node finish being created
            spawn(device.device_node)


if __name__ == "__main__":
    scan_existing()
    monitor_udev()
