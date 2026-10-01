# procontroller-companion

Linux daemon (Debian) that, with any Nintendo Switch Pro Controller connected:

- Runs a configurable command when the capture button (`BTN_Z`) is pressed — defaults to a KDE Spectacle screenshot.
- Runs a custom command on a **double-press of the Home button**.
- **Hotkeys**: while holding the activator **L1 + R2 + Start**, pressing any other button runs the command configured for it in the `[hotkeys]` section. Out of the box: **D-pad up/down** adjusts volume and **L3** toggles mute (PipeWire).

Works with any number of controllers, hotplugged at any time, regardless of player number.

## Install

```bash
sudo ./install.sh
```

Installs under `/usr/local`:

- `/usr/local/bin/procontroller-companion`
- `/usr/local/lib/systemd/system/procontroller-companion.service`
- `/usr/local/etc/procontroller_companion.ini` (only if it doesn't exist yet)

Requires `python3-evdev` and `python3-pyudev` (the installer pulls them via `apt`). The default volume hotkeys use `wpctl` (WirePlumber/PipeWire).

## Uninstall

```bash
sudo ./install.sh uninstall         # keeps the .ini
sudo ./install.sh uninstall purge   # removes the .ini too
```

## Configuration

`/usr/local/etc/procontroller_companion.ini` (or `/etc/procontroller_companion.ini`, which takes priority if present):

```ini
[general]
screenshot_command = spectacle -m -b -n
home_launch = notify-send "Pro Controller" 'home button double-pressed'

[hotkeys]
up_button = wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 5%+
down_button = wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-
l3_button = wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle
```

Every command is passed verbatim to `bash -c "<command>"`: quotes, spaces, pipes, environment variables, etc. all work as in any shell script. They run in the active graphical user's full session environment (D-Bus, display, `XDG_CURRENT_DESKTOP`, `PATH`, ...), not as root. `screenshot_command` defaults to `spectacle -m -b -n` if not set.

### Hotkeys

Hold **L1 + R2 + Start** and press a button. Available keys are every button except Home and the activator's own L1, R2 and Start:

| Key | Button |
|---|---|
| `up_button`, `down_button`, `left_button`, `right_button` | D-pad |
| `a_button`, `b_button`, `x_button`, `y_button` | Face buttons (Nintendo layout) |
| `r1_button`, `l2_button`, `minus_button` | Shoulders / Minus |
| `l3_button`, `r3_button` | Stick clicks |

A missing or empty entry does nothing.

A hotkey runs **once per press**: to run it again you have to release the button and press it again. The only exception is holding it for more than 1 second, after which it repeats every 0.2 s, up to 10 times (handy to drop the volume a lot in one go). Releasing the button or any of the activator buttons stops the repetition. The timings are the `HOLD_REPEAT_*` constants at the top of the script. The capture button keeps using `screenshot_command` in `[general]`.

Note: the daemon only listens to the controller, it doesn't grab it, so games still see the button presses while the chord is held.

Changes to the `.ini` take effect on the next trigger, no service restart needed. If you are upgrading an existing install, the installer leaves your `.ini` untouched: add the `[hotkeys]` section yourself, otherwise the volume controls will stop working.

## Debugging

```bash
sudo systemctl stop procontroller-companion
sudo PROCONTROLLER_DEBUG=1 /usr/local/bin/procontroller-companion
```

Logs every `EV_KEY`/`EV_ABS` event received (code, name, value) and every hotkey run — useful if your driver maps the buttons differently.

```bash
journalctl -u procontroller-companion -f
```

## Requirements

- Debian (or derivative) with `systemd`.
- Kernel with `hid-nintendo` support for the controller.
- `wpctl` (WirePlumber) for the default volume hotkeys.
