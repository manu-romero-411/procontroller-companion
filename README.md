# procontroller-companion

Linux daemon (Debian) that, with any Nintendo Switch Pro Controller connected:

- Maps the capture button (`BTN_Z`) to **Print Screen**.
- Runs a custom command on a **double-press of the Home button**.

Works with any number of controllers, hotplugged at any time, regardless of player number.

## Install

```bash
sudo ./install.sh
```

Installs under `/usr/local`:

- `/usr/local/bin/procontroller-companion`
- `/usr/local/lib/systemd/system/procontroller-companion.service`
- `/usr/local/etc/procontroller_companion.ini` (only if it doesn't exist yet)

Requires `python3-evdev` and `python3-pyudev` (the installer pulls them via `apt`).

## Uninstall

```bash
sudo ./install.sh uninstall         # keeps the .ini
sudo ./install.sh uninstall purge   # removes the .ini too
```

## Configuration

`/usr/local/etc/procontroller_companion.ini` (or `/etc/procontroller_companion.ini`, which takes priority if present):

```ini
[general]
home_launch = notify-send "Pro Controller" 'home button double-pressed'
```

`home_launch` is passed verbatim to `bash -c "<command>"`: quotes, spaces, pipes, environment variables, etc. all work as in any shell script. It runs in the active graphical user's session (D-Bus, display), not as root.

Changes to the `.ini` take effect on the next trigger, no service restart needed.

## Debugging

```bash
sudo systemctl stop procontroller-companion
sudo PROCONTROLLER_DEBUG=1 /usr/local/bin/procontroller-companion
```

Logs every `EV_KEY` event received (code, name, value) — useful if your driver maps the buttons differently.

```bash
journalctl -u procontroller-companion -f
```

## Requirements

- Debian (or derivative) with `systemd`.
- Kernel with `hid-nintendo` support for the controller.
- `/dev/uinput` available (the installer loads the module).
