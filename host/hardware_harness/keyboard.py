"""USB HID keyboard usages and text -> keystroke mapping (US and JIS layouts)."""

from __future__ import annotations

import os
import re

MODIFIERS = {
    "CTRL": 0x01, "SHIFT": 0x02, "ALT": 0x04, "GUI": 0x08,
    "RCTRL": 0x10, "RSHIFT": 0x20, "RALT": 0x40, "RGUI": 0x80,
}
MOD_ALIASES = {"CONTROL": "CTRL", "LCTRL": "CTRL", "LSHIFT": "SHIFT", "LALT": "ALT", "OPTION": "ALT",
               "WIN": "GUI", "CMD": "GUI", "COMMAND": "GUI", "META": "GUI", "SUPER": "GUI",
               "LGUI": "GUI"}

KEYS = {
    "ENTER": 0x28, "ESC": 0x29, "BACKSPACE": 0x2A, "TAB": 0x2B, "SPACE": 0x2C,
    "CAPSLOCK": 0x39, "PRINTSCREEN": 0x46, "SCROLLLOCK": 0x47, "PAUSE": 0x48,
    "INSERT": 0x49, "HOME": 0x4A, "PAGEUP": 0x4B, "DELETE": 0x4C, "END": 0x4D, "PAGEDOWN": 0x4E,
    "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52, "MENU": 0x65,
    # JIS-specific keys
    "RO": 0x87, "KANA": 0x88, "YEN": 0x89, "HENKAN": 0x8A, "MUHENKAN": 0x8B, "ZENKAKU": 0x35,
}
KEYS.update({chr(ord("A") + i): 0x04 + i for i in range(26)})
KEYS.update({str(i): 0x1E + i - 1 for i in range(1, 10)})
KEYS["0"] = 0x27
KEYS.update({f"F{i}": 0x3A + i - 1 for i in range(1, 13)})
KEY_ALIASES = {"RETURN": "ENTER", "ESCAPE": "ESC", "BS": "BACKSPACE", "DEL": "DELETE",
               "INS": "INSERT", "PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "PAGEDN": "PAGEDOWN",
               "HANKAKU": "ZENKAKU", "HANZEN": "ZENKAKU"}

_SHIFT = MODIFIERS["SHIFT"]

# char -> (shift?, usage) for the keys whose meaning differs between layouts
_US = {
    "-": (0, 0x2D), "_": (1, 0x2D), "=": (0, 0x2E), "+": (1, 0x2E), "[": (0, 0x2F), "{": (1, 0x2F),
    "]": (0, 0x30), "}": (1, 0x30), "\\": (0, 0x31), "|": (1, 0x31), ";": (0, 0x33), ":": (1, 0x33),
    "'": (0, 0x34), '"': (1, 0x34), "`": (0, 0x35), "~": (1, 0x35),
    "!": (1, 0x1E), "@": (1, 0x1F), "#": (1, 0x20), "$": (1, 0x21), "%": (1, 0x22), "^": (1, 0x23),
    "&": (1, 0x24), "*": (1, 0x25), "(": (1, 0x26), ")": (1, 0x27),
}
_JIS = {
    "-": (0, 0x2D), "=": (1, 0x2D), "^": (0, 0x2E), "~": (1, 0x2E), "@": (0, 0x2F), "`": (1, 0x2F),
    "[": (0, 0x30), "{": (1, 0x30), "]": (0, 0x32), "}": (1, 0x32), ";": (0, 0x33), "+": (1, 0x33),
    ":": (0, 0x34), "*": (1, 0x34), "\\": (0, 0x87), "_": (1, 0x87), "¥": (0, 0x89), "|": (1, 0x89),
    "!": (1, 0x1E), '"': (1, 0x1F), "#": (1, 0x20), "$": (1, 0x21), "%": (1, 0x22), "&": (1, 0x23),
    "'": (1, 0x24), "(": (1, 0x25), ")": (1, 0x26),
}
_COMMON = {
    " ": (0, 0x2C), "\n": (0, 0x28), "\t": (0, 0x2B),
    ",": (0, 0x36), "<": (1, 0x36), ".": (0, 0x37), ">": (1, 0x37), "/": (0, 0x38), "?": (1, 0x38),
}


def layout() -> str:
    return os.environ.get("HWH_KEYBOARD_LAYOUT", "us").lower()


def char_to_key(ch: str, layout_name: str | None = None) -> tuple[int, int]:
    """(modifier mask, usage) that types `ch`."""
    if "a" <= ch <= "z":
        return 0, KEYS[ch.upper()]
    if "A" <= ch <= "Z":
        return _SHIFT, KEYS[ch]
    if "0" <= ch <= "9":
        return 0, KEYS[ch]
    table = _JIS if (layout_name or layout()) == "jis" else _US
    hit = _COMMON.get(ch) or table.get(ch)
    if hit is None:
        raise ValueError(
            f"can't type {ch!r} with a USB keyboard ({layout_name or layout()} layout); only ASCII "
            "is supported. For Japanese, put the on-screen keyboard in kana mode and type romaji.")
    return (_SHIFT if hit[0] else 0), hit[1]


def parse_combo(combo: str, layout_name: str | None = None) -> tuple[int, list[int]]:
    """"CTRL+SHIFT+A", "ENTER", "F5", "/" -> (modifier mask, [usages])."""
    mods, keys = 0, []
    # split on "+" except a literal one ("+", "SHIFT++")
    for raw in re.split(r"(?<!^)\+(?=.)", combo):
        n = raw.strip().upper().replace("_", "").replace(" ", "") or raw
        n = MOD_ALIASES.get(n, n)
        if n in MODIFIERS:
            mods |= MODIFIERS[n]
            continue
        n = KEY_ALIASES.get(n, n)
        if n in KEYS:
            keys.append(KEYS[n])
        elif len(raw) == 1:
            m, k = char_to_key(raw, layout_name)
            mods |= m
            keys.append(k)
        else:
            raise ValueError(f"unknown key {raw!r}; modifiers={list(MODIFIERS)} keys={list(KEYS)}")
    if len(keys) > 6:
        raise ValueError("at most 6 non-modifier keys at once")
    return mods, keys
