# generic-hardware-harness

Drives any USB-HID host device (game console, PC, ...) from a PC (macOS / Linux / Windows) with an ESP32-S3 (two USB ports: native USB + a USB-UART bridge) and watches it through a UVC HDMI capture card.

The bridge presents itself as a wired gamepad, a keyboard or a mouse. It has been tested with a **Nintendo Switch 2**; the notes below marked *(Switch 2)* are specific to it.

```
PC  ──USB──> [UART port / CH343] ESP32-S3 [native USB / OTG] ──USB──> target device
PC  <──USB── HDMI capture card ("USB Video") <──HDMI── target device
```

- The ESP32-S3 enumerates as a HORIPAD-compatible wired controller (VID 0F0D / PID 0092).
- The host sends controller state to the ESP32 over serial at 921600 baud.
- The serial port is opened exclusively (a second process gets an error instead of silently stealing replies). A dead port, a missed reply or an unexpected bridge restart is recovered automatically: the port is reopened, held inputs are re-sent, and the command is retried once (relative mouse moves and mode switches are never retried).
- If the target device stops talking to the controller (not mounted, or reports stuck) for 5 s while the bus isn't suspended, the firmware detaches and re-attaches USB as if the cable were replugged (`replugs=` in the status). Mode switches also detach USB before restarting. Input sent while unmounted fails with a clear error instead of silently going nowhere. Restarting the MCP server doesn't help with this; if it persists, reseat the cable on the device side or restart the device.
- Frames come from the capture card through OpenCV (AVFoundation on macOS, V4L2 on Linux, the default backend on Windows). If frames stop for 1.5 s (unplugged, card reset, hung read), the device is reopened every 0.3 s until it comes back.

## Requirements

- ESP32-S3 dev board with both a native USB port and a USB-UART bridge port (e.g. ESP32-S3-DevKitC-1 N16R8). For a board with less flash, change `board_build.flash_size` / `partitions` in `firmware/platformio.ini`.
- UVC HDMI capture card.
- [uv](https://docs.astral.sh/uv/). On macOS, `ffmpeg` is used to list capture devices by name.

## Setup

```sh
# Firmware (flashed through the UART port, so the native USB can stay plugged into the device)
cd firmware && uvx --from platformio pio run -t upload
# if the port isn't detected: ... pio run -t upload --upload-port /dev/ttyUSB0 (or COM3)

# Host side
cd host && uv sync
uv run hwh status        # mounted=1 means the device has detected the controller
uv run hwh shot          # screen.jpg
uv run hwh press A
uv run hwh press L+R -d 200
uv run hwh macro "HOME; wait 800; RIGHT; A"
uv run hwh view          # live view window with keyboard control

# Keyboard / mouse (the ESP32 restarts into a different USB device)
uv run hwh mode keyboard # pad | keyboard | mouse (| kbm)
uv run hwh type "mario"  # romaji -> マリオ while the on-screen keyboard is in カナ
uv run hwh key BACKSPACE
uv run hwh mode mouse
uv run hwh mouse move 200 -100
uv run hwh mouse click   # click RIGHT 2 / drag 100 0 / scroll 3
uv run hwh mode pad      # back to the controller
```

*(Switch 2)* On the Switch, turn on **Settings → Controllers & Accessories → Pro Controller Wired Communication**.

Environment variables: `HWH_PORT` (serial port, auto-detected by default: CH34x / CP210x / FTDI / Espressif USB IDs), `HWH_CAMERA` (capture device name or index, default `USB Video`; `hwh devices` lists them on macOS and Linux, Windows needs an index), `HWH_KEYBOARD_LAYOUT` (`us` (default) or `jis`; this affects symbols such as `@ : " _`).

## Keyboard and mouse

The HORIPAD report has no report ID, so it can't share the ESP32's single HID interface with a keyboard or mouse. The bridge therefore has modes, and switching between them restarts the ESP32 (about 2 s). The device sees the old one unplugged and the new one plugged in.

| Mode | USB device |
|---|---|
| `pad` | HORIPAD gamepad (default; a power cycle always returns to this mode) |
| `keyboard` | Keyboard only (no report ID) |
| `mouse` | Mouse only (no report ID) |
| `kbm` | Keyboard (report ID 1) + mouse (report ID 2). *(Switch 2)* **not recognized** (tested 2026-10-24) |

*(Switch 2, tested 2026-10-24)* In `keyboard` mode, typing and Backspace work in the system's on-screen keyboard (opened from the eShop search box). Typing `mario` while the on-screen keyboard is in カナ produces マリオ.

- Text input workflow: open the on-screen keyboard with the gamepad, switch to `mode keyboard`, type, then switch back to `mode pad`. The on-screen keyboard stays open while the mode switches.
- Text input sends ASCII characters only. For Japanese, put the on-screen keyboard in かな/カナ and type romaji.
- *(Switch 2)* The HOME menu doesn't respond to keyboard or mouse. No cursor appears in the system UI in `mouse` mode either, so the mouse should only work in supporting games (not tested).
- The mouse is relative only. The console may accelerate it, so `move` counts don't map exactly to pixels.

## Controlling it from Claude (MCP)

`.mcp.json` is in the repo root. Start Claude Code in this directory and the `hardware` server is available.
Tools: `screenshot`, `press`, `tilt_stick`, `hold`, `release`, `set_stick`, `neutral`, `macro`, `status`, `set_mode`, `type_text`, `key`, `mouse_move`, `mouse_click`, `mouse_drag`, `mouse_scroll`. Most action tools return a screenshot taken after the action.

While the MCP server runs, open http://127.0.0.1:8765 in a browser to watch the screen live, along with a log of every tool call Claude makes and the currently held inputs. `HWH_VIEW_PORT` changes the port (`0` turns it off). `hwh view` can't run at the same time, because the MCP server owns the capture card and the serial port.

## Serial protocol

| Command | Meaning |
|---|---|
| `S <buttons hex> <hat 0-8> <lx> <ly> <rx> <ry>` | Set the full controller state (axes 0-255, 128 = center) |
| `K <mods hex> [usage hex ×6]` | Keyboard state (kbm / keyboard mode) |
| `R <buttons hex> <dx> <dy> [wheel]` | Mouse report, relative values from −127 to 127 (kbm / mouse mode) |
| `N` | Neutral (in any mode) |
| `M <0-3>` | Reply `OK`, then restart in pad / kbm / keyboard / mouse mode |
| `?` | `STATUS mounted= ready= sent= uptime= mode= replugs=` |

Button bits: Y0 B1 A2 X3 L4 R5 ZL6 ZR7 −8 +9 LS10 RS11 HOME12 CAPTURE13. Hat: 0=up, clockwise through 7, 8 = neutral.

## Macros

`A` / `A 300` / `L+R 100` / `wait 500` / `hold ZR` / `release ZR` / `LS 0 -1 400` / `repeat 5 { A 80; wait 200 }` / `neutral`

`mode keyboard` / `type hello world` / `key CTRL+A` / `move 100 -50` / `click` / `click RIGHT 2` / `drag 200 0` / `scroll 3`. The text after `type` can't contain `; { } #`.
