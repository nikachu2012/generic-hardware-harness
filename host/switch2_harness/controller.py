"""Serial client for the ESP32-S3 USB controller bridge."""

from __future__ import annotations

import glob
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import serial

from . import keyboard as kb

try:
    from termios import error as _TermiosError
except ImportError:  # Windows
    _TermiosError = OSError

# What a dead or confused serial link raises (pyserial lets termios.error through as-is;
# TimeoutError, our own "no response", is an OSError)
LINK_ERRORS = (OSError, serial.SerialException, _TermiosError)

# Bit positions in the HORIPAD report
BUTTONS = {
    "Y": 0, "B": 1, "A": 2, "X": 3,
    "L": 4, "R": 5, "ZL": 6, "ZR": 7,
    "MINUS": 8, "PLUS": 9, "LCLICK": 10, "RCLICK": 11,
    "HOME": 12, "CAPTURE": 13,
}
ALIASES = {"-": "MINUS", "+": "PLUS", "L3": "LCLICK", "R3": "RCLICK", "LS": "LCLICK",
           "RS": "RCLICK", "SELECT": "MINUS", "START": "PLUS", "CAP": "CAPTURE"}

# D-pad -> hat value
HAT = {
    "UP": 0, "UPRIGHT": 1, "RIGHT": 2, "DOWNRIGHT": 3,
    "DOWN": 4, "DOWNLEFT": 5, "LEFT": 6, "UPLEFT": 7,
}
HAT_ALIASES = {"U": "UP", "D": "DOWN", "LEFT": "LEFT", "RIGHT": "RIGHT", "UR": "UPRIGHT",
               "DR": "DOWNRIGHT", "DL": "DOWNLEFT", "UL": "UPLEFT",
               "DUP": "UP", "DDOWN": "DOWN", "DLEFT": "LEFT", "DRIGHT": "RIGHT"}
HAT_NEUTRAL = 8

# USB personalities of the bridge (switching one restarts the ESP32, ~2 s)
MODES = {"pad": 0, "kbm": 1, "keyboard": 2, "mouse": 3}
MODE_ALIASES = {"gamepad": "pad", "controller": "pad", "km": "kbm", "kb": "keyboard"}

MOUSE_BUTTONS = {"LEFT": 1, "RIGHT": 2, "MIDDLE": 4, "BACK": 8, "FORWARD": 16}


def _axis(v: float) -> int:
    """-1.0..1.0 -> 0..255 (128 = center)."""
    v = max(-1.0, min(1.0, float(v)))
    return max(0, min(255, round(128 + v * 127.5)))


@dataclass
class State:
    buttons: set[str] = field(default_factory=set)
    hat: int = HAT_NEUTRAL
    lx: float = 0.0
    ly: float = 0.0  # +1 = down (screen convention)
    rx: float = 0.0
    ry: float = 0.0

    def to_command(self) -> str:
        mask = 0
        for b in self.buttons:
            mask |= 1 << BUTTONS[b]
        return (f"S {mask:04x} {self.hat} {_axis(self.lx)} {_axis(self.ly)} "
                f"{_axis(self.rx)} {_axis(self.ry)}\n")


def normalize(name: str) -> tuple[str, str]:
    """Return ("button", NAME) or ("hat", NAME) for a user supplied input name."""
    n = name.strip().upper().replace("_", "").replace(" ", "")
    n = ALIASES.get(n, n)
    if n in BUTTONS:
        return "button", n
    n = HAT_ALIASES.get(n, n)
    if n in HAT:
        return "hat", n
    raise ValueError(f"unknown input {name!r}; buttons={list(BUTTONS)} dpad={list(HAT)}")


# USB-UART bridges commonly found on ESP32-S3 boards (WCH CH34x, Silicon Labs CP210x,
# FTDI) plus Espressif's own USB-Serial/JTAG.
_BRIDGE_VIDS = {0x1A86, 0x10C4, 0x0403, 0x303A}


def find_port() -> str:
    env = os.environ.get("S2H_PORT")
    if env:
        return env
    try:
        from serial.tools import list_ports

        ports = sorted(list_ports.comports(), key=lambda p: p.device)
        for p in ports:
            if p.vid in _BRIDGE_VIDS:
                return p.device
    except Exception:
        pass
    for pat in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*",
                "/dev/ttyACM*", "/dev/ttyUSB*"):
        found = sorted(glob.glob(pat))
        if found:
            return found[0]
    raise RuntimeError("ESP32 serial port not found; set S2H_PORT or pass --port")


class NotMountedError(RuntimeError):
    """The bridge is fine but the console isn't enumerating it."""


class Controller:
    """Serial link to the bridge. Recovers on its own from a dead port (USB re-enumeration),
    missed replies and an unexpected bridge restart, so a long-running MCP server keeps working."""

    def __init__(self, port: str | None = None, baud: int = 921600):
        self._port_arg = port
        self._baud = baud
        self.port = port or find_port()
        self._ser: serial.Serial | None = None
        self._lock = threading.RLock()
        self.state = State()
        self.mode = "pad"
        self._mouse_buttons = 0
        self.reconnects = 0
        self._mounted_at = 0.0  # last time the console was seen talking to the bridge
        with self._lock:
            self._connect(timeout=5.0)

    def _open(self) -> None:
        s = serial.Serial()
        s.port = self.port = self._port_arg or find_port()
        s.baudrate = self._baud
        s.timeout = 1.0
        # A second process on the same port would steal replies; fail loudly instead.
        s.exclusive = True
        # Leave DTR/RTS at their default (both asserted): the auto-reset circuit only
        # fires when they differ, and a reset would unplug the controller from the Switch.
        s.open()
        self._ser = s
        time.sleep(0.05)
        s.reset_input_buffer()

    def _close_port(self) -> None:
        ser, self._ser = self._ser, None  # drop it even if close() fails on a dead fd
        if ser is not None:
            try:
                ser.close()
            except LINK_ERRORS:
                # keep its __del__ from closing an fd number a new port may have reused
                ser.fd, ser.is_open = None, False

    def _connect(self, timeout: float) -> dict:
        """(Re)open the port and wait until the bridge answers. Caller holds the lock."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self._ser is None:
                    self._open()
                return self._parse_status(self._raw("?\n"))  # the bridge may still be booting
            except (*LINK_ERRORS, RuntimeError) as e:  # RuntimeError: port not found
                self._close_port()
                if time.monotonic() > deadline:
                    raise ConnectionError(f"bridge not reachable on {self.port}: {e}") from e
                time.sleep(0.3)

    def _reconnect(self, why: Exception) -> None:
        print(f"bridge: {type(why).__name__}: {why}; reconnecting", file=sys.stderr)
        self._close_port()
        self._connect(timeout=10.0)
        if self.mode == "pad":
            self._raw(self.state.to_command())  # restore held inputs
        self.reconnects += 1
        print(f"bridge: reconnected (#{self.reconnects}) on {self.port}", file=sys.stderr)

    def close(self) -> None:
        try:
            self.neutral()
        finally:
            self._close_port()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- low level -------------------------------------------------------
    def _raw(self, line: str) -> str:
        """One request/response round trip, no recovery. Caller holds the lock."""
        if self._ser is None or not self._ser.is_open:
            raise serial.SerialException("port is not open")
        self._ser.reset_input_buffer()  # a late reply to an earlier command would desync us
        self._ser.write(line.encode())
        while True:
            resp = self._ser.readline().decode(errors="replace").strip()
            if not resp:
                raise TimeoutError(f"no response from bridge for {line.strip()!r}")
            if resp.startswith(("OK", "ERR", "STATUS")):
                return resp
            # skip boot banners etc.

    def _check_mounted(self, wait: float = 3.0) -> None:
        """Raise if the console isn't talking to the bridge. The firmware happily accepts input
        while unmounted (it just goes nowhere), so check before sending, at most once a second."""
        if time.monotonic() - self._mounted_at < 1.0:
            return
        deadline = time.monotonic() + wait  # e.g. still enumerating after a mode switch
        while True:
            st = self.status()
            if st.get("mounted"):
                self._mounted_at = time.monotonic()
                return
            if time.monotonic() > deadline:
                break
            time.sleep(0.2)
        hint = ("the bridge re-plugs USB every 5 s on its own (replugs=%d so far)" % st["replugs"]
                if "replugs" in st else "update the firmware for automatic USB re-plug")
        raise NotMountedError(
            f"the Switch is not talking to the controller (mounted=0, mode={self.mode}); {hint}. "
            "Restarting the MCP server or sending signals does not help. If it persists, reseat "
            "the ESP32's native-USB cable on the Switch side or restart the console.")

    def _cmd(self, line: str, retry: bool = True) -> str:
        """Send a command; on a dead port or missing reply, reconnect and send it once more.
        Pass retry=False for commands that must not run twice (relative mouse moves, restarts)."""
        if line[0] in "SKR":
            self._check_mounted()
        with self._lock:
            try:
                resp = self._raw(line)
            except LINK_ERRORS as e:
                if not retry:
                    raise
                self._reconnect(e)
                resp = self._raw(line)
        if resp.startswith("ERR"):
            if resp == "ERR send failed":
                self._mounted_at = 0.0
                self._check_mounted(wait=0)
            if resp.startswith("ERR not in") and retry:
                expected = self.mode
                try:
                    self.status(retry=False)
                except LINK_ERRORS:
                    pass
                if self.mode != expected:
                    raise RuntimeError(f"{resp}: the bridge restarted in {self.mode} mode")
            raise RuntimeError(resp)
        return resp

    def send(self) -> None:
        self._cmd(self.state.to_command())

    def status(self, retry: bool = True) -> dict:
        return self._parse_status(self._cmd("?\n", retry=retry))

    def _parse_status(self, resp: str) -> dict:
        st = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", resp)}
        # firmware without mode support reports no mode: it's always a gamepad
        self.mode = next(k for k, v in MODES.items() if v == st.get("mode", 0))
        return st

    def set_mode(self, mode: str, timeout: float = 10.0) -> dict:
        """Switch the USB personality: "pad", "kbm" (keyboard+mouse), "keyboard" or "mouse".
        The ESP32 restarts and the Switch sees the old device unplugged and the new one plugged in."""
        name = MODE_ALIASES.get(mode.strip().lower(), mode.strip().lower())
        if name not in MODES:
            raise ValueError(f"unknown mode {mode!r}; one of {list(MODES)}")
        if name == self.mode:
            return self.status()
        self._cmd(f"M {MODES[name]}\n", retry=False)
        self.state = State()
        self._mouse_buttons = 0
        deadline = time.monotonic() + timeout
        time.sleep(0.5)
        while True:
            try:
                st = self.status(retry=False)  # _raw drops the ROM boot log
                if self.mode == name:
                    return st
            except (*LINK_ERRORS, RuntimeError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"bridge did not come back in {name} mode")
            time.sleep(0.2)

    # --- state manipulation ----------------------------------------------
    def neutral(self) -> None:
        self.state = State()
        self._mouse_buttons = 0
        self._cmd("N\n")

    def hold(self, *names: str) -> None:
        for name in names:
            kind, n = normalize(name)
            if kind == "button":
                self.state.buttons.add(n)
            else:
                self.state.hat = HAT[n]
        self.send()

    def release(self, *names: str) -> None:
        for name in names:
            kind, n = normalize(name)
            if kind == "button":
                self.state.buttons.discard(n)
            elif self.state.hat == HAT[n]:
                self.state.hat = HAT_NEUTRAL
        self.send()

    def set_stick(self, stick: str, x: float, y: float) -> None:
        """stick='L' or 'R'; x,y in -1..1, y positive = down."""
        if stick.upper().startswith("L"):
            self.state.lx, self.state.ly = x, y
        else:
            self.state.rx, self.state.ry = x, y
        self.send()

    # --- convenience -----------------------------------------------------
    def press(self, *names: str, duration: float = 0.1, after: float = 0.05) -> None:
        """Press inputs together for `duration` seconds, then release and wait `after`."""
        self.hold(*names)
        time.sleep(duration)
        self.release(*names)
        if after:
            time.sleep(after)

    def tilt(self, stick: str, x: float, y: float, duration: float = 0.3, after: float = 0.05) -> None:
        self.set_stick(stick, x, y)
        time.sleep(duration)
        self.set_stick(stick, 0, 0)
        if after:
            time.sleep(after)

    # --- keyboard ("kbm" / "keyboard" mode) --------------------------------
    def key_down(self, mods: int, usages: list[int]) -> None:
        self._cmd("K " + " ".join(f"{v:x}" for v in [mods, *usages]) + "\n")

    def key_up(self) -> None:
        self._cmd("K 0\n")

    def key(self, combo: str, duration: float = 0.05, after: float = 0.03) -> None:
        """Tap a key or chord: "ENTER", "CTRL+A", "SHIFT+TAB", "F5", "/"."""
        mods, usages = kb.parse_combo(combo)
        self.key_down(mods, usages)
        time.sleep(duration)
        self.key_up()
        if after:
            time.sleep(after)

    def type_text(self, text: str, interval: float = 0.03) -> None:
        """Type ASCII text ("\n" = Enter). Layout follows S2H_KEYBOARD_LAYOUT (us / jis)."""
        strokes = [kb.char_to_key(ch) for ch in text]  # validate everything before typing
        for mods, usage in strokes:
            self.key_down(mods, [usage])
            time.sleep(interval)
            self.key_up()
            time.sleep(interval)

    # --- mouse ("kbm" / "mouse" mode) ----------------------------------------
    @staticmethod
    def _mouse_mask(button: str) -> int:
        b = button.strip().upper()
        if b not in MOUSE_BUTTONS:
            raise ValueError(f"unknown mouse button {button!r}; one of {list(MOUSE_BUTTONS)}")
        return MOUSE_BUTTONS[b]

    def _mouse(self, dx: int = 0, dy: int = 0, wheel: int = 0) -> None:
        # relative report: retry only when it carries no motion, so a move never runs twice
        self._cmd(f"R {self._mouse_buttons:x} {dx} {dy} {wheel}\n", retry=not (dx or dy or wheel))

    def mouse_move(self, dx: int, dy: int, step: int = 40, interval: float = 0.008) -> None:
        """Move the pointer by (dx, dy) counts, +y = down, split into reports of <= `step` per axis.
        The console may apply acceleration, so counts are not exactly pixels."""
        step = max(1, min(127, step))
        n = max(1, -(-max(abs(dx), abs(dy)) // step))
        sent_x = sent_y = 0
        for i in range(1, n + 1):
            x, y = round(dx * i / n), round(dy * i / n)
            self._mouse(x - sent_x, y - sent_y)
            sent_x, sent_y = x, y
            if i < n:
                time.sleep(interval)

    def mouse_down(self, button: str = "LEFT") -> None:
        self._mouse_buttons |= self._mouse_mask(button)
        self._mouse()

    def mouse_up(self, button: str = "LEFT") -> None:
        self._mouse_buttons &= ~self._mouse_mask(button)
        self._mouse()

    def click(self, button: str = "LEFT", count: int = 1, duration: float = 0.05,
              interval: float = 0.08) -> None:
        for i in range(count):
            self.mouse_down(button)
            time.sleep(duration)
            self.mouse_up(button)
            if i < count - 1:
                time.sleep(interval)

    def drag(self, dx: int, dy: int, button: str = "LEFT", step: int = 20) -> None:
        self.mouse_down(button)
        time.sleep(0.05)
        try:
            self.mouse_move(dx, dy, step=step, interval=0.016)
            time.sleep(0.05)
        finally:
            self.mouse_up(button)

    def scroll(self, clicks: int, interval: float = 0.03) -> None:
        """Scroll the wheel by `clicks` notches; positive = down (like stick y)."""
        for _ in range(abs(clicks)):
            self._mouse(wheel=-1 if clicks > 0 else 1)
            time.sleep(interval)

    def run_macro(self, script: str) -> None:
        """Run a tiny macro language. Statements separated by newlines or ';':

            A                 press A (100 ms)
            A 300             press A for 300 ms
            L+R 100           press L and R together
            wait 500          sleep 500 ms
            hold ZR / release ZR
            LS 0 -1 400       tilt left stick up for 400 ms (also RS)
            repeat 5 { A 80; wait 200 }
            neutral
            mode kbm          switch USB personality (pad / kbm / keyboard / mouse)
            type hello world  type text (no ; { } # inside)
            key CTRL+A        tap a key or chord
            move 100 -50      move the mouse (+y = down)
            click / click RIGHT 2 / drag 200 0 / scroll 3
        """
        _run(self, _parse(script))


# --- macro parser --------------------------------------------------------
def _tokenize(script: str) -> list[str]:
    script = re.sub(r"#[^\n]*", "", script)
    return re.findall(r"\{|\}|;|\n|[^\s;{}]+(?:[ \t]+[^\s;{}]+)*", script)


def _parse(script: str):
    toks = [t.strip() for t in _tokenize(script)]
    pos = 0

    def block(top: bool):
        nonlocal pos
        out = []
        while pos < len(toks):
            t = toks[pos]
            pos += 1
            if t in ("", ";", "\n"):
                continue
            if t == "}":
                if top:
                    raise ValueError("unexpected }")
                return out
            if t == "{":
                raise ValueError("unexpected {")
            words = t.split()
            if words[0].lower() == "repeat":
                n = int(words[1])
                while pos < len(toks) and toks[pos] in ("", "\n"):
                    pos += 1
                if pos >= len(toks) or toks[pos] != "{":
                    raise ValueError("repeat needs { ... }")
                pos += 1
                out.append(("repeat", n, block(False)))
            else:
                out.append(("stmt", words, t))
        if not top:
            raise ValueError("missing }")
        return out

    return block(True)


def _run(c: Controller, prog) -> None:
    for node in prog:
        if node[0] == "repeat":
            for _ in range(node[1]):
                _run(c, node[2])
            continue
        words = node[1]
        op = words[0].lower()
        if op in ("wait", "sleep"):
            time.sleep(float(words[1]) / 1000)
        elif op == "hold":
            c.hold(*words[1:])
        elif op == "release":
            c.release(*words[1:])
        elif op == "neutral":
            c.neutral()
        elif op == "mode":
            c.set_mode(words[1])
        elif op == "type":
            c.type_text(node[2].split(None, 1)[1] if len(words) > 1 else "")
        elif op == "key":
            c.key(words[1])
        elif op == "move":
            c.mouse_move(int(words[1]), int(words[2]))
        elif op == "click":
            c.click(words[1] if len(words) > 1 else "LEFT", int(words[2]) if len(words) > 2 else 1)
        elif op == "drag":
            c.drag(int(words[1]), int(words[2]), words[3] if len(words) > 3 else "LEFT")
        elif op == "scroll":
            c.scroll(int(words[1]))
        elif op in ("ls", "rs"):
            x, y = float(words[1]), float(words[2])
            ms = float(words[3]) if len(words) > 3 else 300
            c.tilt(op[0], x, y, duration=ms / 1000, after=0)
        else:
            names = words[0].split("+") if words[0] not in ("+", "-") else [words[0]]
            ms = float(words[1]) if len(words) > 1 else 100
            c.press(*names, duration=ms / 1000, after=0.05)
