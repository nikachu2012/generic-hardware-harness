"""MCP server: lets an AI agent see the target device's screen and press buttons."""

from __future__ import annotations

import functools
import inspect
import os
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP, Image

from . import viewer
from .capture import Capture
from .controller import BUTTONS, HAT, Controller

mcp = FastMCP(
    "hardware",
    instructions=(
        "Controls a USB-HID host device (game console, PC, ...) through a USB gamepad emulator and shows its screen via a "
        "capture card. Inputs: buttons " + ", ".join(BUTTONS) + "; d-pad " + ", ".join(HAT) +
        ". Stick y is +1 = down. Most action tools return a screenshot taken after the action; "
        "look at it before deciding the next input. The bridge is one USB device at a time: "
        "mode 'pad' (gamepad: press / tilt_stick / hold), 'keyboard' (type_text / key) or "
        "'mouse' (mouse_*). Use set_mode to switch; it takes ~2 s and looks like an unplug/replug "
        "to the device. For text, open the on-screen keyboard with the gamepad first, then switch "
        "to 'keyboard' and type (romaji becomes kana when the on-screen keyboard is in kana mode); "
        "switch back to 'pad' afterwards. The mouse only works in games that support it. "
        "Mode 'kbm' (both) is not recognized by some devices (e.g. Nintendo Switch 2)."
    ),
)

_ctl: Controller | None = None
_cap: Capture | None = None
_init_lock = threading.Lock()  # the live viewer calls these from its own threads


def ctl() -> Controller:
    global _ctl
    with _init_lock:
        if _ctl is None:
            _ctl = Controller()
    return _ctl


def cap() -> Capture:
    global _cap
    with _init_lock:
        if _cap is None:
            _cap = Capture()
    return _cap


def _logged(fn):
    """Record each tool call in the live viewer's action log."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        call_args = {k: v for k, v in bound.arguments.items()
                     if v != sig.parameters[k].default or k in ("inputs", "script", "stick")}
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            viewer.log_event(fn.__name__, call_args, f"{type(e).__name__}: {e}", error=True)
            raise
        texts = [o for o in (out if isinstance(out, list) else [out]) if isinstance(o, (str, dict))]
        viewer.log_event(fn.__name__, call_args, " ".join(map(str, texts)))
        return out

    return wrapper


def _view_state() -> str:
    if _ctl is None:
        return "controller not connected yet"
    if _ctl.mode != "pad":
        return f"mode: {_ctl.mode}"
    s = _ctl.state
    held = "+".join(sorted(s.buttons)) or "-"
    hat = next((k for k, v in HAT.items() if v == s.hat), "-")
    return f"held: {held}  dpad: {hat}  L({s.lx:+.1f},{s.ly:+.1f}) R({s.rx:+.1f},{s.ry:+.1f})"


def _shot(width: int) -> Image:
    return Image(data=cap().jpeg(width=width), format="jpeg")


def _result(msg: str, wait_ms: int, screenshot: bool, width: int):
    if wait_ms:
        time.sleep(wait_ms / 1000)
    if not screenshot:
        return msg
    return [msg, _shot(width)]


@mcp.tool()
@_logged
def screenshot(width: int = 1280) -> Image:
    """Return the current screen of the target device as JPEG (resized to `width` px, full res is 1920)."""
    return _shot(width)


@mcp.tool()
@_logged
def press(inputs: str, duration_ms: int = 100, wait_ms: int = 400, screenshot: bool = True,
          width: int = 1280):
    """Press one or more inputs together, e.g. "A", "HOME", "L+R", "DOWN".
    Waits `wait_ms` after release, then returns a screenshot (if `screenshot`)."""
    names = [n for n in inputs.replace(",", "+").split("+") if n.strip()] or [inputs]
    ctl().press(*names, duration=duration_ms / 1000, after=0)
    return _result(f"pressed {'+'.join(names)} for {duration_ms}ms", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def tilt_stick(stick: str, x: float, y: float, duration_ms: int = 300, wait_ms: int = 300,
               screenshot: bool = True, width: int = 1280):
    """Tilt stick "L" or "R" to (x, y), each -1..1 (y=+1 is down), for `duration_ms`, then recenter."""
    ctl().tilt(stick, x, y, duration=duration_ms / 1000, after=0)
    return _result(f"tilted {stick} to ({x},{y}) for {duration_ms}ms", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def hold(inputs: str) -> str:
    """Start holding inputs (e.g. "ZR" or "B+RIGHT") until `release` or `neutral` is called."""
    names = [n for n in inputs.split("+") if n.strip()]
    ctl().hold(*names)
    return f"holding {sorted(ctl().state.buttons)} hat={ctl().state.hat}"


@mcp.tool()
@_logged
def release(inputs: str) -> str:
    """Release previously held inputs."""
    ctl().release(*[n for n in inputs.split("+") if n.strip()])
    return f"holding {sorted(ctl().state.buttons)} hat={ctl().state.hat}"


@mcp.tool()
@_logged
def set_stick(stick: str, x: float, y: float) -> str:
    """Set stick "L"/"R" to (x, y) and keep it there (use 0,0 to recenter)."""
    ctl().set_stick(stick, x, y)
    return f"{stick} stick at ({x},{y})"


@mcp.tool()
@_logged
def neutral() -> str:
    """Release every button and center both sticks."""
    ctl().neutral()
    return "neutral"


@mcp.tool()
@_logged
def macro(script: str, wait_ms: int = 300, screenshot: bool = True, width: int = 1280):
    """Run an input macro. Statements separated by ';' or newlines:
    `A` (tap 100ms), `A 300` (hold 300ms), `L+R 100`, `wait 500`, `hold ZR`, `release ZR`,
    `LS 0 -1 400` (left stick up 400ms), `RS 1 0 200`, `repeat 5 { A 80; wait 200 }`, `neutral`.
    Keyboard/mouse: `mode keyboard`, `type some text`, `key CTRL+A`, `move 100 -50`, `click`,
    `click RIGHT 2`, `drag 200 0`, `scroll 3`."""
    t0 = time.monotonic()
    ctl().run_macro(script)
    return _result(f"macro done in {time.monotonic() - t0:.2f}s", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def set_mode(mode: str, wait_ms: int = 1500, screenshot: bool = True, width: int = 1280):
    """Switch the USB device the target sees: "pad" (gamepad), "keyboard" or "mouse".
    ("kbm" = keyboard + mouse in one device exists but some devices (e.g. Switch 2) don't recognize it.)"""
    st = ctl().set_mode(mode)
    return _result(f"mode {ctl().mode} {st}", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def type_text(text: str, enter: bool = False, wait_ms: int = 300, screenshot: bool = True,
              width: int = 1280):
    """Type ASCII text on the USB keyboard (needs mode "keyboard"; "\n" = Enter).
    Set `enter` to press Enter afterwards. For Japanese, put the on-screen keyboard in かな/カナ and type romaji."""
    ctl().type_text(text)
    if enter:
        ctl().key("ENTER")
    return _result(f"typed {len(text)} chars" + (" + ENTER" if enter else ""), wait_ms,
                   screenshot, width)


@mcp.tool()
@_logged
def key(keys: str, wait_ms: int = 300, screenshot: bool = True, width: int = 1280):
    """Tap a keyboard key or chord, e.g. "ENTER", "BACKSPACE", "CTRL+A", "SHIFT+TAB", "UP", "F1".
    Needs mode "keyboard"."""
    ctl().key(keys)
    return _result(f"key {keys}", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def mouse_move(dx: int, dy: int, wait_ms: int = 200, screenshot: bool = True, width: int = 1280):
    """Move the mouse pointer by (dx, dy) counts, +dy = down. Relative only and the console may
    accelerate, so move, look at the screenshot, then correct. Needs mode "mouse"."""
    ctl().mouse_move(dx, dy)
    return _result(f"moved ({dx},{dy})", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def mouse_click(button: str = "LEFT", count: int = 1, wait_ms: int = 300, screenshot: bool = True,
                width: int = 1280):
    """Click a mouse button (LEFT, RIGHT, MIDDLE, BACK, FORWARD); count=2 double-clicks."""
    ctl().click(button, count)
    return _result(f"clicked {button} x{count}", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def mouse_drag(dx: int, dy: int, button: str = "LEFT", wait_ms: int = 200, screenshot: bool = True,
               width: int = 1280):
    """Hold `button`, move by (dx, dy), release."""
    ctl().drag(dx, dy, button)
    return _result(f"dragged ({dx},{dy}) with {button}", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def mouse_scroll(clicks: int, wait_ms: int = 300, screenshot: bool = True, width: int = 1280):
    """Turn the mouse wheel by `clicks` notches; positive = down."""
    ctl().scroll(clicks)
    return _result(f"scrolled {clicks}", wait_ms, screenshot, width)


@mcp.tool()
@_logged
def status() -> dict:
    """Bridge status: mounted=1 means the target has enumerated the device;
    mode 0=pad, 1=kbm, 2=keyboard, 3=mouse."""
    return ctl().status()


def main() -> None:
    port = int(os.environ.get("HWH_VIEW_PORT", "8765"))
    if port:
        try:
            viewer.start(port, lambda: cap().jpeg(width=960, quality=70, fresh=False), _view_state)
        except OSError as e:
            print(f"hardware-harness live view disabled: {e}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
