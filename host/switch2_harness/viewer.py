"""Browser live view for the MCP server: MJPEG stream of the screen plus a log of agent actions.

Runs inside the MCP server process because that process owns the capture card and the
serial port. Open http://127.0.0.1:8765 (env S2H_VIEW_PORT, 0 disables it).
"""

from __future__ import annotations

import collections
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

_events: collections.deque[dict] = collections.deque(maxlen=200)
_seq = 0
_events_lock = threading.Lock()


def log_event(tool: str, args: dict, result: str, error: bool = False) -> None:
    global _seq
    with _events_lock:
        _seq += 1
        _events.append({"id": _seq, "t": time.time(), "tool": tool, "args": args,
                        "result": result, "error": error})


def events_since(after: int) -> list[dict]:
    with _events_lock:
        return [e for e in _events if e["id"] > after]


PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switch 2 Live</title>
<style>
:root { --bg:#111; --fg:#ddd; --dim:#888; --accent:#e60012; --panel:#1b1b1b; --err:#ff6b6b; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 -apple-system, sans-serif;
       display:flex; height:100vh; }
#screen { flex:1; display:flex; align-items:center; justify-content:center; min-width:0; }
#screen img { max-width:100%; max-height:100vh; }
aside { width:360px; background:var(--panel); display:flex; flex-direction:column; }
header { padding:10px 12px; border-bottom:1px solid #333; }
header b { color:var(--accent); }
#state { color:var(--dim); font:12px ui-monospace, monospace; margin-top:4px; }
#log { flex:1; overflow-y:auto; padding:6px 0; font:12px ui-monospace, monospace; }
.e { padding:4px 12px; border-left:3px solid transparent; }
.e.new { border-left-color:var(--accent); background:#2a1416; }
.e .t { color:var(--dim); }
.e .r { color:var(--dim); white-space:pre-wrap; }
.e.err .r { color:var(--err); }
@media (max-width: 800px) { body { flex-direction:column; height:auto; } aside { width:auto; height:50vh; } }
</style></head>
<body>
<div id="screen"><img src="/stream" alt="Switch 2 screen"></div>
<aside>
  <header><b>Switch 2</b> live · <span id="last">waiting for agent…</span>
    <div id="state"></div></header>
  <div id="log"></div>
</aside>
<script>
let after = 0;
const log = document.getElementById('log');
function fmtArgs(a) {
  return Object.entries(a).map(([k, v]) => k + '=' + JSON.stringify(v)).join(' ');
}
async function poll() {
  try {
    const r = await fetch('/events?after=' + after);
    const d = await r.json();
    for (const e of d.events) {
      after = e.id;
      const div = document.createElement('div');
      div.className = 'e new' + (e.error ? ' err' : '');
      const t = new Date(e.t * 1000).toLocaleTimeString();
      div.innerHTML = '<span class="t"></span> <b></b> <span class="a"></span><div class="r"></div>';
      div.querySelector('.t').textContent = t;
      div.querySelector('b').textContent = e.tool;
      div.querySelector('.a').textContent = fmtArgs(e.args);
      div.querySelector('.r').textContent = e.result;
      log.prepend(div);
      setTimeout(() => div.classList.remove('new'), 1500);
      document.getElementById('last').textContent = t + ' ' + e.tool;
    }
    while (log.children.length > 200) log.lastChild.remove();
    document.getElementById('state').textContent = d.state;
  } catch (e) {}
  setTimeout(poll, 500);
}
poll();
</script></body></html>
"""


def start(port: int, get_jpeg: Callable[[], bytes], get_state: Callable[[], str],
          fps: float = 15) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # stdout/stderr noise would interfere with MCP stdio
            pass

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/stream":
                self._stream()
            elif path == "/events":
                after = 0
                for kv in query.split("&"):
                    k, _, v = kv.partition("=")
                    if k == "after" and v.isdigit():
                        after = int(v)
                try:
                    state = get_state()
                except Exception as e:  # noqa: BLE001
                    state = f"({e})"
                body = json.dumps({"events": events_since(after), "state": state}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            interval = 1 / fps
            try:
                while True:
                    t0 = time.monotonic()
                    try:
                        jpg = get_jpeg()
                    except Exception:  # noqa: BLE001  capture not ready / unplugged
                        time.sleep(1)
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    time.sleep(max(0.0, interval - (time.monotonic() - t0)))
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"switch2 live view: http://127.0.0.1:{port}", file=sys.stderr)
    return srv
