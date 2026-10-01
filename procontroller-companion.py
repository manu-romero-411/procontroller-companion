#!/usr/bin/env python3
"""
procontroller-companion

Daemon for Nintendo Switch Pro Controllers connected to a Linux PC:

  1. Runs a user-configured shell command when the capture button
     (BTN_Z) is pressed (screenshot_command, [general] section).
  2. Runs a user-configured shell command when the Home button
     (BTN_MODE) is pressed twice in quick succession (home_launch).
  3. Hotkeys: while the activator chord L1 + R2 + Start is held, pressing
     any other button runs the command configured for it in the [hotkeys]
     section (a_button, b_button, up_button, l3_button, ...). A hotkey runs
     once per press; it only repeats if the button stays held for more than
     HOLD_REPEAT_DELAY, and then at most HOLD_REPEAT_MAX times.

Works with any number of controllers, hotplugged at any time, regardless
of player number.

Dependencies (Debian):
    sudo apt install python3-evdev python3-pyudev

Config file (see procontroller_companion.ini), checked in this order:
    /etc/procontroller_companion.ini
    /usr/local/etc/procontroller_companion.ini

    [general]
    home_launch = <shell command>
    screenshot_command = <shell command>

    [hotkeys]
    up_button = <shell command>
    ...

All commands are passed verbatim to `bash -c "<command>"`, so quoting,
spaces, pipes, redirects and environment variables all follow normal
bash rules. They run as the user with the active graphical session
(full copy of that user's environment: DISPLAY/WAYLAND_DISPLAY,
DBUS_SESSION_BUS_ADDRESS, XDG_CURRENT_DESKTOP, PATH, etc.), not as root.

Requires access to /dev/input/event* -> intended to run as a systemd
service (see procontroller-companion.service).
"""

import configparser
import os
import subprocess
import threading
import time
import evdev
import pyudev
from evdev import ecodes as e

TARGET_NAME_SUBSTR = "Pro Controller"

CAPTURE_CODE = e.BTN_Z       # capture button, mapped by the hid-nintendo driver
HOME_CODE = e.BTN_MODE       # home button (double-press launcher only)

# Activator chord: all of these must be held for hotkeys to fire.
L1_CODE = e.BTN_TL           # L1 shoulder button
R2_CODE = e.BTN_TR2          # ZR / R2
START_CODE = e.BTN_START     # + button
CHORD = frozenset({L1_CODE, R2_CODE, START_CODE})

# Configurable hotkeys: [hotkeys] key name -> evdev button code.
# Excluded on purpose: home and the three chord buttons (L1, R2, Start).
# Nintendo layout: A is the right button (BTN_EAST), B the bottom one, etc.
HOTKEY_BUTTONS = {
    "a_button": e.BTN_EAST,
    "b_button": e.BTN_SOUTH,
    "x_button": e.BTN_NORTH,
    "y_button": e.BTN_WEST,
    "r1_button": e.BTN_TR,
    "l2_button": e.BTN_TL2,
    "minus_button": e.BTN_SELECT,
    "l3_button": e.BTN_THUMBL,
    "r3_button": e.BTN_THUMBR,
}
CODE_TO_HOTKEY = {code: name for name, code in HOTKEY_BUTTONS.items()}

# D-pad is reported as two hat axes. Keys: up_button, down_button,
# left_button, right_button.
DPAD_X_CODE = e.ABS_HAT0X    # -1 left, 0 released, 1 right
DPAD_Y_CODE = e.ABS_HAT0Y    # -1 up, 0 released, 1 down
DPAD_NAMES = {
    ("x", -1): "left_button",
    ("x", 1): "right_button",
    ("y", -1): "up_button",
    ("y", 1): "down_button",
}
DPAD_NAME_SET = frozenset(DPAD_NAMES.values())

DOUBLE_PRESS_WINDOW = 0.4    # seconds between Home presses to count as a double press
# A hotkey fires once per press. Only if the button stays held longer than
# HOLD_REPEAT_DELAY does it repeat, every HOLD_REPEAT_INTERVAL, at most
# HOLD_REPEAT_MAX extra times (e.g. to drop the volume a lot in one go).
HOLD_REPEAT_DELAY = 1.0      # seconds held before repeating starts
HOLD_REPEAT_INTERVAL = 0.2   # seconds between repeats
HOLD_REPEAT_MAX = 10         # maximum number of repeats per press

SESSION_CACHE_TTL = 5.0      # seconds to reuse the detected user session/env

DEFAULT_SCREENSHOT_COMMAND = "spectacle -m -b -n"

CONFIG_PATHS = [
    "/etc/procontroller_companion.ini",
    "/usr/local/etc/procontroller_companion.ini",
]

# Set PROCONTROLLER_DEBUG=1 in the environment (or in the systemd unit) to log
# every EV_KEY/EV_ABS event with its symbolic name, e.g. to confirm which code a
# given button actually sends on this controller/driver version.
DEBUG = bool(os.environ.get("PROCONTROLLER_DEBUG"))

active = {}
active_lock = threading.Lock()

_session_cache = {"t": 0.0, "val": None}
_session_lock = threading.Lock()


def load_config_value(key, fallback=None, section="general"):
    cp = configparser.ConfigParser(interpolation=None)
    for path in CONFIG_PATHS:
        if os.path.isfile(path):
            cp.read(path)
            return cp.get(section, key, fallback=fallback)
    return fallback


def get_active_graphical_session():
    """Return (username, uid) of the user with an active seat session,
    so commands run in their session instead of root's."""
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


def find_user_session_env(uid):
    """Return a full copy of the environment of a running process owned
    by uid that looks like part of a graphical session (has DISPLAY or
    WAYLAND_DISPLAY set). This carries over XDG_CURRENT_DESKTOP,
    DBUS_SESSION_BUS_ADDRESS, PATH, LANG, etc. instead of guessing which
    handful of variables a given command might need."""
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
                return env_vars
    except Exception:
        pass
    return {}


def get_session_context():
    """Return (username, uid, env) for the active graphical session, or
    None. Cached for a few seconds: D-pad hotkeys repeat several times per
    second and scanning /proc every time would be wasteful."""
    with _session_lock:
        now = time.monotonic()
        if _session_cache["val"] is not None and now - _session_cache["t"] < SESSION_CACHE_TTL:
            return _session_cache["val"]

        session = get_active_graphical_session()
        if not session:
            _session_cache["val"] = None
            return None

        username, uid = session
        uid = int(uid)
        env = find_user_session_env(uid)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        bus_path = f"/run/user/{uid}/bus"
        if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(bus_path):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"

        _session_cache["val"] = (username, uid, env)
        _session_cache["t"] = now
        return _session_cache["val"]


def run_in_user_session(cmd, log_label, quiet=False):
    if not cmd:
        if not quiet:
            print(f"[!] {log_label}: no command configured", flush=True)
        return

    ctx = get_session_context()
    if not ctx:
        print(f"[!] {log_label}: no active graphical session found, running without one", flush=True)
        subprocess.Popen(["/bin/bash", "-c", cmd])
        return

    username, _uid, env = ctx
    if not quiet:
        print(f"[+] {log_label} -> running as {username}: {cmd}", flush=True)
    env_args = [f"{k}={v}" for k, v in env.items()]
    subprocess.Popen(["runuser", "-u", username, "--", "env", "-i", *env_args, "bash", "-c", cmd])


def trigger_home_action():
    run_in_user_session(load_config_value("home_launch"), "Home double-press")


def trigger_screenshot_action():
    cmd = load_config_value("screenshot_command", DEFAULT_SCREENSHOT_COMMAND)
    run_in_user_session(cmd, "Capture button")


def trigger_hotkey(name):
    """Run the [hotkeys] command for `name` (e.g. "up_button"). A missing
    or empty entry simply does nothing. Quiet unless debugging, since
    held hotkeys repeat and would flood the journal."""
    cmd = load_config_value(name, section="hotkeys")
    if not cmd:
        if DEBUG:
            print(f"[debug] hotkey {name}: nothing configured", flush=True)
        return
    run_in_user_session(cmd, f"Hotkey {name}", quiet=not DEBUG)


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
    held = set()                   # currently held chord buttons
    dpad = {"x": 0, "y": 0}
    lock = threading.Lock()
    # Hotkey currently held down, if any. Only a fresh press made while the
    # chord is active sets it; releasing the button (or the chord) clears it.
    cur = {"name": None, "next": 0.0, "count": 0}
    stop_flag = threading.Event()

    def chord_active():
        return CHORD <= held

    def dpad_held_names():
        return {DPAD_NAMES[(axis, 1 if v > 0 else -1)]
                for axis, v in dpad.items() if v != 0}

    def press(name):
        with lock:
            cur["name"] = name
            cur["next"] = time.monotonic() + HOLD_REPEAT_DELAY
            cur["count"] = 0
        trigger_hotkey(name)

    def hold_repeater():
        while not stop_flag.is_set():
            name = None
            with lock:
                if cur["name"] is not None:
                    now = time.monotonic()
                    if not chord_active() or cur["count"] >= HOLD_REPEAT_MAX:
                        cur["name"] = None
                    elif now >= cur["next"]:
                        name = cur["name"]
                        cur["count"] += 1
                        cur["next"] = now + HOLD_REPEAT_INTERVAL
            if name:
                trigger_hotkey(name)
            time.sleep(0.05)

    repeater_thread = threading.Thread(target=hold_repeater, daemon=True)
    repeater_thread.start()

    try:
        for event in dev.read_loop():
            if DEBUG and event.type == e.EV_KEY:
                name = e.keys.get(event.code, event.code)
                print(f"[debug] {dev.name}: code={event.code} name={name} value={event.value}", flush=True)

            if DEBUG and event.type == e.EV_ABS:
                name = e.ABS.get(event.code, event.code)
                print(f"[debug] {dev.name}: ABS code={event.code} name={name} value={event.value}", flush=True)

            if event.type == e.EV_KEY:
                if event.code in CHORD:
                    if event.value in (1, 2):
                        held.add(event.code)
                    else:
                        held.discard(event.code)

                elif event.code == CAPTURE_CODE and event.value == 1:
                    trigger_screenshot_action()

                elif event.code == HOME_CODE and event.value == 1:
                    now = time.monotonic()
                    if now - last_home_press <= DOUBLE_PRESS_WINDOW:
                        last_home_press = 0.0
                        trigger_home_action()
                    else:
                        last_home_press = now

                elif event.code in CODE_TO_HOTKEY:
                    name = CODE_TO_HOTKEY[event.code]
                    if event.value == 1 and chord_active():
                        press(name)
                    elif event.value == 0:
                        with lock:
                            if cur["name"] == name:
                                cur["name"] = None

            elif event.type == e.EV_ABS and event.code in (DPAD_X_CODE, DPAD_Y_CODE):
                axis = "x" if event.code == DPAD_X_CODE else "y"
                dpad[axis] = event.value
                if event.value != 0:
                    if chord_active():
                        press(DPAD_NAMES[(axis, 1 if event.value > 0 else -1)])
                else:
                    with lock:
                        if cur["name"] in DPAD_NAME_SET and cur["name"] not in dpad_held_names():
                            cur["name"] = None
    except OSError:
        print(f"[-] {path} disconnected", flush=True)
    finally:
        stop_flag.set()
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
    while True:
        try:
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by(subsystem="input")
            print("[*] Watching for controller connect/disconnect events...", flush=True)
            for device in iter(monitor.poll, None):
                if device.action == "add" and device.device_node and "event" in device.device_node:
                    time.sleep(0.3)  # let the node finish being created
                    spawn(device.device_node)
        except Exception as exc:
            print(f"[!] udev monitor error: {exc}, retrying in 5s...", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    scan_existing()
    monitor_udev()
