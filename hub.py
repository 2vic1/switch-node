#!/usr/bin/env python3
"""
Switch Node — hub.

Telegram is the terminal. Every machine running `node_agent.py` becomes a node
you can drive from your phone.

Design notes
------------
* stdlib only — no pip install, no node_modules, runs anywhere Python 3.9+ runs.
* The hub owns the bot token, the job queue (SQLite) and the node registry.
* Nodes never talk to Telegram. They poll the hub over HTTP for work, so a node
  behind NAT still works without port forwarding as long as it can reach the hub.
* Everything is append-only in `data/hub.db`, so a hub restart loses nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "hub.db"
CONFIG_PATH = ROOT / "config.json"

TG_API = "https://api.telegram.org/bot{token}/{method}"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        cfg = {
            "bot_token": os.environ.get("SWITCH_BOT_TOKEN", ""),
            "owner_id": int(os.environ.get("SWITCH_OWNER_ID", "0") or 0),
            "node_secret": secrets.token_urlsafe(24),
            "hub_host": "0.0.0.0",
            "hub_port": 8787,
            "job_timeout": 300,
            "max_output": 3500,
        }
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"[hub] wrote {CONFIG_PATH} — put your bot token in it and restart")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CFG = load_config()
TOKEN = CFG.get("bot_token", "")
OWNER = int(CFG.get("owner_id") or 0)
SECRET = CFG["node_secret"]
MAX_OUT = int(CFG.get("max_output", 3500))


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                name        TEXT PRIMARY KEY,
                host        TEXT,
                platform    TEXT,
                last_seen   REAL,
                registered  REAL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node        TEXT NOT NULL,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL,
                chat_id     INTEGER,
                status      TEXT NOT NULL DEFAULT 'queued',
                created     REAL,
                started     REAL,
                finished    REAL,
                output      TEXT,
                exit_code   INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_node_status ON jobs(node, status);
            """
        )


def upsert_node(name: str, host: str, platform: str) -> None:
    now = time.time()
    with db() as conn:
        conn.execute(
            """INSERT INTO nodes(name, host, platform, last_seen, registered)
               VALUES(?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 host=excluded.host, platform=excluded.platform, last_seen=excluded.last_seen""",
            (name, host, platform, now, now),
        )


def touch_node(name: str) -> None:
    with db() as conn:
        conn.execute("UPDATE nodes SET last_seen=? WHERE name=?", (time.time(), name))


def queue_job(node: str, kind: str, payload: str, chat_id: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(node,kind,payload,chat_id,status,created) VALUES(?,?,?,?, 'queued', ?)",
            (node, kind, payload, chat_id, time.time()),
        )
        return int(cur.lastrowid)


def take_job(node: str) -> sqlite3.Row | None:
    """Atomically claim the oldest queued job for this node."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE node=? AND status='queued' ORDER BY id LIMIT 1", (node,)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
        return row


def finish_job(job_id: int, output: str, exit_code: int) -> sqlite3.Row | None:
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', finished=?, output=?, exit_code=? WHERE id=?",
            (time.time(), output, exit_code, job_id),
        )
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def get_job(job_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #
def tg(method: str, payload: dict | None = None, timeout: int = 40) -> dict:
    if not TOKEN:
        raise RuntimeError("bot_token is empty in config.json")
    url = TG_API.format(token=TOKEN, method=method)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tg_send(chat_id: int, text: str, markdown: bool = False) -> None:
    for chunk_start in range(0, max(len(text), 1), 4000):
        chunk = text[chunk_start : chunk_start + 4000] or "(empty)"
        try:
            tg(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown" if markdown else None,
                    "disable_web_page_preview": True,
                },
            )
        except urllib.error.HTTPError as e:
            # Markdown can choke on odd characters; fall back to plain text once.
            body = e.read().decode(errors="ignore")
            if "parse entities" in body and markdown:
                tg("sendMessage", {"chat_id": chat_id, "text": chunk})
            else:
                print("[hub] sendMessage failed:", body[:200])


def tg_send_photo(chat_id: int, png_b64: str, caption: str = "") -> bool:
    try:
        raw = base64.b64decode(png_b64)
    except Exception:
        return False
    boundary = "----switch" + secrets.token_hex(8)
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
            f"filename=\"shot.png\"\r\nContent-Type: image/png\r\n\r\n"
        ).encode()
        + raw
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        TG_API.format(token=TOKEN, method="sendPhoto"),
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()).get("ok", False)
    except Exception as e:
        print("[hub] sendPhoto failed:", e)
        return False


# --------------------------------------------------------------------------- #
# command handling
# --------------------------------------------------------------------------- #
HELP = """*Switch Node* — your machines, from your phone.

/nodes — who is online
/status — one line per node
/run <node> <command> — run a shell command
/agent <node> <prompt> — hand a prompt to a coding agent on that node
/shot <node> — screenshot that node
/log <job\\_id> — full output of a job
/jobs — last 10 jobs
/stop <node> — cancel queued jobs

Add a node: `python node_agent.py --name laptop --hub http://<this-host>:8787`"""


def nodes_table() -> str:
    with db() as conn:
        rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
    if not rows:
        return "No nodes registered yet.\nStart one with `python node_agent.py --name laptop`"
    now = time.time()
    lines = ["*Nodes*", ""]
    for r in rows:
        age = now - (r["last_seen"] or 0)
        dot = "🟢" if age < 45 else ("🟡" if age < 180 else "🔴")
        lines.append(f"{dot} `{r['name']}` — {r['platform']} · seen {int(age)}s ago")
    return "\n".join(lines)


def jobs_table(limit: int = 10) -> str:
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No jobs yet."
    out = ["*Recent jobs*", ""]
    for r in rows:
        icon = {"queued": "⏳", "running": "▶️", "done": "✅", "failed": "❌"}.get(r["status"], "•")
        out.append(f"{icon} #{r['id']} `{r['node']}` {r['kind']} — {r['payload'][:60]}")
    return "\n".join(out)


def handle_text(chat_id: int, text: str) -> None:
    global OWNER
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower().split("@")[0] if parts else ""

    if OWNER == 0:
        # first contact becomes the owner
        OWNER = chat_id
        CFG["owner_id"] = chat_id
        CONFIG_PATH.write_text(json.dumps(CFG, indent=2), encoding="utf-8")
        tg_send(chat_id, f"Owner registered: `{chat_id}`", markdown=True)

    if chat_id != OWNER:
        tg_send(chat_id, "This bot is private.")
        return

    if cmd in ("/start", "/help"):
        tg_send(chat_id, HELP, markdown=True)
        return

    if cmd in ("/nodes", "/status"):
        tg_send(chat_id, nodes_table(), markdown=True)
        return

    if cmd == "/jobs":
        tg_send(chat_id, jobs_table(), markdown=True)
        return

    if cmd in ("/run", "/agent", "/shot", "/stop"):
        if cmd == "/shot":
            if len(parts) < 2:
                tg_send(chat_id, "Usage: `/shot <node>`", markdown=True)
                return
            node = parts[1]
            jid = queue_job(node, "shot", "", chat_id)
            tg_send(chat_id, f"📸 screenshot queued on `{node}` — job #{jid}", markdown=True)
            return

        if cmd == "/stop":
            if len(parts) < 2:
                tg_send(chat_id, "Usage: `/stop <node>`", markdown=True)
                return
            node = parts[1]
            with db() as conn:
                n = conn.execute(
                    "UPDATE jobs SET status='failed', output='cancelled' "
                    "WHERE node=? AND status='queued'",
                    (node,),
                ).rowcount
            tg_send(chat_id, f"Cancelled {n} queued job(s) on `{node}`", markdown=True)
            return

        if len(parts) < 3:
            tg_send(chat_id, f"Usage: `{cmd} <node> <text>`", markdown=True)
            return
        node, body = parts[1], parts[2]
        kind = "shell" if cmd == "/run" else "agent"
        jid = queue_job(node, kind, body, chat_id)
        tg_send(chat_id, f"⏳ job #{jid} → `{node}`\n`{body[:120]}`", markdown=True)
        return

    if cmd == "/log":
        if len(parts) < 2:
            tg_send(chat_id, "Usage: `/log <job_id>`", markdown=True)
            return
        with db() as conn:
            r = conn.execute("SELECT * FROM jobs WHERE id=?", (parts[1].lstrip("#"),)).fetchone()
        if not r:
            tg_send(chat_id, "No such job.")
            return
        tg_send(
            chat_id,
            f"#{r['id']} `{r['node']}` {r['status']} exit={r['exit_code']}\n\n{r['output'] or '(no output)'}",
            markdown=True,
        )
        return

    tg_send(chat_id, "Unknown command. /help")


# --------------------------------------------------------------------------- #
# telegram poller
# --------------------------------------------------------------------------- #
def poll_telegram() -> None:
    offset = 0
    while True:
        try:
            resp = tg("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]}, timeout=40)
            for upd in resp.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text")
                if chat_id and text:
                    threading.Thread(target=handle_text, args=(chat_id, text), daemon=True).start()
        except Exception as e:
            print("[hub] poll error:", e)
            time.sleep(3)


# --------------------------------------------------------------------------- #
# node http api
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep the console quiet
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _authed(self, body: dict) -> bool:
        return body.get("secret") == SECRET

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/api/nodes":
            with db() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM nodes").fetchall()]
            self._json(200, {"ok": True, "nodes": rows})
        elif self.path.startswith("/static/"):
            self._static(self.path[len("/static/"):])
        else:
            self._json(404, {"ok": False})

    # Provisioning a new node used to mean copying files over by hand. Serving
    # the agent from the hub turns it into one curl on the target machine.
    def _static(self, name: str) -> None:
        name = name.split("?")[0]
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self._json(404, {"ok": False})
            return
        if not name.endswith((".py", ".sh", ".json")):
            self._json(404, {"ok": False})
            return
        path = ROOT / name
        if not path.is_file():
            self._json(404, {"ok": False})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = self._body()
        if not self._authed(body):
            self._json(403, {"ok": False, "error": "bad secret"})
            return

        if self.path == "/api/register":
            name = body.get("name") or "unnamed"
            upsert_node(name, body.get("host", ""), body.get("platform", ""))
            self._json(200, {"ok": True, "node": name})

        elif self.path == "/api/poll":
            name = body.get("name") or "unnamed"
            touch_node(name)
            deadline = time.time() + 25
            while time.time() < deadline:
                job = take_job(name)
                if job:
                    self._json(200, {"ok": True, "job": dict(job)})
                    return
                time.sleep(0.7)
            touch_node(name)
            self._json(200, {"ok": True, "job": None})

        elif self.path == "/api/result":
            jid = int(body["job_id"])
            pending = get_job(jid)
            png = body.get("png_b64")
            output = body.get("output", "")[:20000]

            # A screenshot with no chat behind it belongs to the MCP server, so
            # land the file and record its path *before* the job turns 'done' —
            # otherwise a client polling for the result can read the old output.
            if pending and pending["kind"] == "shot" and png and not pending["chat_id"]:
                shots = DATA / "shots"
                shots.mkdir(exist_ok=True)
                path = shots / f"{jid}.png"
                try:
                    path.write_bytes(base64.b64decode(png))
                    output = str(path)
                except Exception as e:
                    print("[hub] could not save screenshot:", e)

            job = finish_job(jid, output, int(body.get("exit_code", 0)))
            if job:
                chat_id = job["chat_id"]
                if not chat_id:
                    pass  # queued over the API, not from a chat — nobody to notify
                elif job["kind"] == "shot" and png:
                    if not tg_send_photo(chat_id, png, f"{job['node']} · job #{job['id']}"):
                        tg_send(chat_id, "Screenshot failed to upload.")
                else:
                    head = f"✅ #{job['id']} `{job['node']}` exit={job['exit_code']}\n\n"
                    out = (job["output"] or "(no output)")[:MAX_OUT]
                    tail = "" if len(job["output"] or "") <= MAX_OUT else "\n…truncated, /log for full"
                    tg_send(chat_id, head + "```\n" + out + "\n```" + tail, markdown=True)
            self._json(200, {"ok": True})

        elif self.path == "/api/queue":
            node = body.get("node") or ""
            kind = body.get("kind") or "shell"
            if not node:
                self._json(400, {"ok": False, "error": "node is required"})
                return
            if kind not in ("shell", "agent", "shot"):
                self._json(400, {"ok": False, "error": f"unknown kind {kind}"})
                return
            jid = queue_job(node, kind, body.get("payload", ""), 0)
            self._json(200, {"ok": True, "job_id": jid})

        elif self.path == "/api/job":
            row = get_job(int(body.get("job_id", 0)))
            if not row:
                self._json(404, {"ok": False, "error": "no such job"})
                return
            self._json(200, {"ok": True, "job": dict(row)})

        elif self.path == "/api/cancel":
            node = body.get("node") or ""
            with db() as conn:
                n = conn.execute(
                    "UPDATE jobs SET status='failed', output='cancelled' "
                    "WHERE node=? AND status='queued'",
                    (node,),
                ).rowcount
            self._json(200, {"ok": True, "cancelled": n})

        else:
            self._json(404, {"ok": False})


# --------------------------------------------------------------------------- #
def console_send(chat_id: int, text: str, markdown: bool = False) -> None:
    """Stand-in for tg_send in --chat mode: print what the bot would say."""
    print(text, flush=True)


def console_send_photo(chat_id: int, png_b64: str, caption: str = "") -> bool:
    print(f"[photo] {caption or 'screenshot'}", flush=True)
    return True


def chat_loop() -> None:
    """Drive the command handler from stdin — the chat path without Telegram."""
    global OWNER
    OWNER = 1
    print("chat mode — type /status, /run <node> <cmd>, /shot <node>, /help. Ctrl-C quits.")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        handle_text(1, line)
        print("", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", help="run one command as the owner, then exit (for testing)")
    ap.add_argument("--chat", action="store_true", help="drive the commands from stdin instead of Telegram")
    args = ap.parse_args()

    init_db()

    if args.simulate:
        handle_text(OWNER or 1, args.simulate)
        return

    if args.chat:
        # Results come back through the HTTP server thread, so the console
        # printers have to be installed before the server starts.
        globals()["tg_send"] = console_send
        globals()["tg_send_photo"] = console_send_photo
        host, port = CFG.get("hub_host", "0.0.0.0"), int(CFG.get("hub_port", 8787))
        srv = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[hub] listening on http://{host}:{port} (chat mode, no Telegram)")
        try:
            chat_loop()
        except KeyboardInterrupt:
            pass
        print("[hub] bye")
        return

    if not TOKEN:
        print("[hub] no bot token — edit config.json")
        sys.exit(1)

    threading.Thread(target=poll_telegram, daemon=True).start()

    host, port = CFG.get("hub_host", "0.0.0.0"), int(CFG.get("hub_port", 8787))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"[hub] listening on http://{host}:{port}")
    print(f"[hub] node secret: {SECRET}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[hub] bye")


if __name__ == "__main__":
    main()
