#!/usr/bin/env python3
"""
Switch Node — demo stream.

Runs `demo.py` and streams its real stdout into a browser page, so the demo can
be watched (or screen-recorded) without a terminal window. Nothing here is
staged: every line the page shows is a byte that came out of the running
process.

    python serve_demo.py
    # then open http://127.0.0.1:8899

The demo starts three seconds after the page connects.
"""

from __future__ import annotations

import html
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8899

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Switch Node — demo</title>
<style>
  html, body {
    margin: 0; height: 100%;
    background: #0d1117; color: #e6edf3;
    font-family: "Cascadia Mono", "Consolas", ui-monospace, monospace;
  }
  #wrap {
    padding: 46px 56px; font-size: 20px; line-height: 1.62;
    white-space: pre-wrap; word-break: break-word;
  }
  .dim { color: #7d8590; }
  .ok { color: #3fb950; }
  .warn { color: #d29922; }
  .path { color: #58a6ff; }
  #cursor {
    display: inline-block; width: 11px; height: 21px;
    background: #e6edf3; vertical-align: -3px;
    animation: blink 1.1s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
</style>
</head>
<body><div id="wrap"><span id="out"></span><span id="cursor"></span></div>
<script>
  const out = document.getElementById("out");
  const wrap = document.getElementById("wrap");
  const es = new EventSource("/stream");
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.done) { es.close(); return; }
    let t = document.createTextNode(d.text + "\\n");
    out.appendChild(t);
    wrap.scrollTop = wrap.scrollHeight;
    window.scrollTo(0, document.body.scrollHeight);
  };
  es.onerror = () => es.close();
</script>
</body>
</html>
"""


def colourise(line: str) -> str:
    """Server-side is plain; the page keeps it plain too. No ANSI needed."""
    return line


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self._run()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, payload: str) -> None:
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    def _run(self) -> None:
        import json

        # a short beat before the first line, so a recording starts on a clean page
        for i in (3, 2, 1):
            self._send(json.dumps({"text": f"  starting in {i}…"}))
            time.sleep(1)
        self._send(json.dumps({"text": "\x1b"}))

        p = subprocess.Popen(
            [sys.executable, "-u", "demo.py"],
            cwd=str(HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert p.stdout is not None
        for line in p.stdout:
            self._send(json.dumps({"text": line.rstrip("\n")}))
        p.wait()
        self._send(json.dumps({"done": True}))


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"demo page: http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
