"""Command line entry point: `s2h <command>`."""

from __future__ import annotations

import argparse
import time


def _viewer(args) -> None:
    import cv2

    from .capture import Capture
    from .controller import Controller

    # waitKeyEx codes for arrow keys: macOS, Linux (GTK/Qt), Windows
    keymap = {
        63232: "UP", 63233: "DOWN", 63234: "LEFT", 63235: "RIGHT",
        65362: "UP", 65364: "DOWN", 65361: "LEFT", 65363: "RIGHT",
        2490368: "UP", 2621440: "DOWN", 2424832: "LEFT", 2555904: "RIGHT",
        ord("a"): "A", ord("b"): "B", ord("x"): "X", ord("y"): "Y", 13: "A",
        ord("q"): "L", ord("e"): "R", ord("1"): "ZL", ord("3"): "ZR",
        ord("+"): "PLUS", ord("="): "PLUS", ord("-"): "MINUS",
        ord("h"): "HOME", ord("c"): "CAPTURE",
    }
    sticks = {ord("i"): (0, -1), ord("k"): (0, 1), ord("j"): (-1, 0), ord("l"): (1, 0)}

    with Controller(args.port) as c, Capture(args.camera) as cap:
        print("keys: arrows=D-pad a/b/x/y Enter=A q/e=L/R 1/3=ZL/ZR +/- h=HOME c=CAPTURE "
              "ijkl=L stick  p=save screenshot  Esc=quit")
        while True:
            f = cap.frame()
            cv2.imshow("Switch 2", cv2.resize(f, (1280, 720)))
            k = cv2.waitKeyEx(15)
            if k == -1:
                continue
            if k == 27:
                break
            if k in keymap:
                c.press(keymap[k], duration=0.08, after=0)
            elif k in sticks:
                c.tilt("L", *sticks[k], duration=0.2, after=0)
            elif k == ord("p"):
                path = time.strftime("shot-%Y%m%d-%H%M%S.jpg")
                cv2.imwrite(path, f)
                print("saved", path)
        cv2.destroyAllWindows()


def main() -> None:
    p = argparse.ArgumentParser(prog="s2h", description="Switch 2 harness")
    p.add_argument("--port", help="ESP32 serial port (env S2H_PORT)")
    p.add_argument("--camera", help="capture device name or index (env S2H_CAMERA, default 'USB Video')")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="bridge status")
    sp = sub.add_parser("press", help="press inputs, e.g. `s2h press A` or `s2h press L+R -d 200`")
    sp.add_argument("inputs")
    sp.add_argument("-d", "--duration", type=int, default=100, help="ms")
    sm = sub.add_parser("macro", help="run a macro script (string or @file)")
    sm.add_argument("script")
    ss = sub.add_parser("shot", help="save a screenshot")
    ss.add_argument("path", nargs="?", default="screen.jpg")
    smode = sub.add_parser("mode", help="show or switch the USB mode: pad / kbm / keyboard / mouse")
    smode.add_argument("mode", nargs="?")
    st = sub.add_parser("type", help="type text on the USB keyboard (kbm/keyboard mode)")
    st.add_argument("text")
    st.add_argument("--enter", action="store_true", help="press Enter afterwards")
    sk = sub.add_parser("key", help="tap a key or chord, e.g. ENTER, CTRL+A (kbm/keyboard mode)")
    sk.add_argument("keys")
    smo = sub.add_parser("mouse", help="mouse (kbm/mouse mode): move DX DY | click [BUTTON] [N] | "
                                       "drag DX DY | scroll N")
    smo.add_argument("action", choices=["move", "click", "drag", "scroll"])
    smo.add_argument("args", nargs="*")
    sub.add_parser("view", help="live view window with keyboard control")
    sub.add_parser("devices", help="list capture devices")
    sub.add_parser("mcp", help="run the MCP server (stdio)")
    args = p.parse_args()

    if args.cmd == "devices":
        from .capture import list_devices
        devices = list_devices()
        if not devices:
            print("device names aren't available on this platform; use an index (0, 1, ...)")
        for i, n in devices:
            print(i, n)
    elif args.cmd == "shot":
        from .capture import Capture
        with Capture(args.camera) as cap:
            print(cap.save(args.path))
    elif args.cmd == "view":
        _viewer(args)
    elif args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
    else:
        from .controller import Controller
        with Controller(args.port) as c:
            if args.cmd == "status":
                print(c.status(), c.mode)
            elif args.cmd == "mode":
                if args.mode:
                    c.set_mode(args.mode)
                print(c.mode)
            elif args.cmd == "type":
                c.type_text(args.text)
                if args.enter:
                    c.key("ENTER")
            elif args.cmd == "key":
                c.key(args.keys)
            elif args.cmd == "mouse":
                a = args.args
                if args.action == "move":
                    c.mouse_move(int(a[0]), int(a[1]))
                elif args.action == "click":
                    c.click(a[0] if a else "LEFT", int(a[1]) if len(a) > 1 else 1)
                elif args.action == "drag":
                    c.drag(int(a[0]), int(a[1]), a[2] if len(a) > 2 else "LEFT")
                else:
                    c.scroll(int(a[0]))
            elif args.cmd == "press":
                c.press(*args.inputs.split("+"), duration=args.duration / 1000)
            elif args.cmd == "macro":
                script = args.script
                if script.startswith("@"):
                    with open(script[1:]) as fh:
                        script = fh.read()
                c.run_macro(script)


if __name__ == "__main__":
    main()
