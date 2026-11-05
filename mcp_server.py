#!/usr/bin/env python3
"""
Switch Node — MCP server.

Exposes the machines behind a Switch Node hub as MCP tools, so an agent that
already lives in a chat app (the one Switch connected) can reach every other
machine you own without leaving the conversation.

    Switch  ──chat──▶  agent  ──MCP stdio──▶  this  ──HTTP──▶  hub  ──▶  nodes

It is a thin adapter on purpose: the hub owns the queue and the registry, this
process only speaks MCP and blocks until a job comes back.

Run it as an MCP stdio server (Switch Console, Claude Code, Cursor, Codex and
OpenCode all speak this):

    python mcp_server.py

Configuration is read from config.json next to this file, or from
SWITCH_HUB_URL / SWITCH_NODE_SECRET.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "switch-node"
SERVER_VERSION = "0.2.0"


# --------------------------------------------------------------------------- #
# hub client
# --------------------------------------------------------------------------- #
def _cfg() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


CFG = _cfg()
HUB = os.environ.get("SWITCH_HUB_URL") or f"http://{CFG.get('hub_host', '127.0.0.1')}:{CFG.get('hub_port', 8787)}"
if HUB in ("http://0.0.0.0:8787", "http://0.0.0.0"):
    HUB = "http://127.0.0.1:8787"
SECRET = os.environ.get("SWITCH_NODE_SECRET") or CFG.get("node_secret", "")


def call(path: str, payload: dict | None = None, timeout: int = 60) -> dict:
    url = HUB.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def api(path: str, payload: dict | None = None, timeout: int = 60) -> dict:
    body = dict(payload or {})
    body["secret"] = SECRET
    return call(path, body, timeout)


def nodes() -> list[dict]:
    # /api/nodes is a plain GET; the secret rides in the query so one code path
    # covers it without a second helper.
    try:
        with urllib.request.urlopen(f"{HUB.rstrip('/')}/api/nodes", timeout=15) as r:
            return json.loads(r.read().decode()).get("nodes", [])
    except Exception:
        return []


def node_names() -> list[str]:
    return [n["name"] for n in nodes()]


def run_job(node: str, kind: str, payload: str, wait: float) -> dict:
    """Queue a job and block until it finishes (or `wait` seconds pass)."""
    known = node_names()
    if known and node not in known:
        return {
            "error": f"unknown node {node!r}. Known nodes: {', '.join(known)}",
            "isError": True,
        }
    try:
        queued = api("/api/queue", {"node": node, "kind": kind, "payload": payload})
    except urllib.error.URLError as e:
        return {"error": f"hub unreachable at {HUB}: {e}", "isError": True}
    jid = queued.get("job_id")
    if not jid:
        return {"error": f"hub refused the job: {queued}", "isError": True}

    deadline = time.time() + wait
    last = None
    while time.time() < deadline:
        try:
            last = api("/api/job", {"job_id": jid})["job"]
        except Exception:
            time.sleep(1)
            continue
        if last["status"] in ("done", "failed"):
            return {
                "job_id": jid,
                "node": node,
                "kind": kind,
                "status": last["status"],
                "exit_code": last["exit_code"],
                "output": last.get("output") or "",
            }
        time.sleep(0.6)

    return {
        "job_id": jid,
        "node": node,
        "kind": kind,
        "status": "still running",
        "note": f"job #{jid} did not finish within {int(wait)}s — it keeps running on the node; "
                f"call job_status with job_id={jid} to collect it later",
    }


def render(result: dict) -> str:
    if "error" in result:
        return result["error"]
    head = f"#{result['job_id']} on {result['node']} · {result['status']}"
    if result.get("exit_code") is not None:
        head += f" · exit {result['exit_code']}"
    out = result.get("output") or "(no output)"
    if result.get("note"):
        head += f"\n{result['note']}"
    return f"{head}\n\n{out}"


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
TOOLS = [
    {
        "name": "list_nodes",
        "description": (
            "List the machines this hub can reach, with platform and how many seconds ago "
            "each was last seen. Call this first to learn which node names are valid."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command on a named machine and return its stdout, stderr and exit code. "
            "Use this for anything you would type into that machine's terminal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node name, as returned by list_nodes."},
                "command": {"type": "string", "description": "Shell command to run on that machine."},
                "wait_seconds": {
                    "type": "integer",
                    "description": "How long to wait for the result before returning a job id. Default 120.",
                    "default": 120,
                },
            },
            "required": ["node", "command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent_task",
        "description": (
            "Hand a natural-language task to the coding agent installed on a remote machine "
            "(codebuff, opencode, claude or codex — whichever it has). Use this when the work "
            "needs judgement rather than an exact command."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node name."},
                "prompt": {"type": "string", "description": "What the agent on that machine should do."},
                "wait_seconds": {"type": "integer", "default": 300},
            },
            "required": ["node", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "screenshot_node",
        "description": (
            "Capture the desktop of a remote machine and save the PNG on the hub. "
            "Returns the file path so you can attach or inspect it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "Node name."}},
            "required": ["node"],
            "additionalProperties": False,
        },
    },
    {
        "name": "job_status",
        "description": "Fetch a job by id — use it to collect a job that was still running when it was queued.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "integer", "description": "Job id."}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_queued",
        "description": "Cancel every job still waiting in the queue for a node.",
        "inputSchema": {
            "type": "object",
            "properties": {"node": {"type": "string", "description": "Node name."}},
            "required": ["node"],
            "additionalProperties": False,
        },
    },
]


def call_tool(name: str, args: dict) -> tuple[str, bool]:
    if name == "list_nodes":
        ns = nodes()
        if not ns:
            return "No nodes registered. Start one with: python node_agent.py --name <name> --hub <url>", False
        now = time.time()
        lines = []
        for n in ns:
            age = int(now - (n.get("last_seen") or 0))
            dot = "online" if age < 45 else ("idle" if age < 180 else "offline")
            lines.append(f"{n['name']} · {n.get('platform', '?')} · {dot} ({age}s ago)")
        return "\n".join(lines), False

    if name == "run_command":
        r = run_job(args["node"], "shell", args["command"], float(args.get("wait_seconds", 120)))
        return render(r), bool(r.get("isError"))

    if name == "agent_task":
        r = run_job(args["node"], "agent", args["prompt"], float(args.get("wait_seconds", 300)))
        return render(r), bool(r.get("isError"))

    if name == "screenshot_node":
        r = run_job(args["node"], "shot", "", float(args.get("wait_seconds", 90)))
        if r.get("isError"):
            return render(r), True
        return render(r), False

    if name == "job_status":
        try:
            job = api("/api/job", {"job_id": int(args["job_id"])})["job"]
        except Exception as e:
            return f"could not read job: {e}", True
        return render(
            {
                "job_id": job.get("id"),
                "node": job.get("node"),
                "kind": job.get("kind"),
                "status": job.get("status"),
                "exit_code": job.get("exit_code"),
                "output": job.get("output"),
            }
        ), False

    if name == "cancel_queued":
        try:
            res = api("/api/cancel", {"node": args["node"]})
        except Exception as e:
            return f"could not cancel: {e}", True
        return f"cancelled {res.get('cancelled', 0)} queued job(s) on {args['node']}", False

    return f"unknown tool {name!r}", True


# --------------------------------------------------------------------------- #
# json-rpc plumbing (MCP stdio: one JSON object per line)
# --------------------------------------------------------------------------- #
def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(mid, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def fail(mid, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def handle(msg: dict) -> None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    # notifications carry no id and must never be answered
    if mid is None:
        return

    if method == "initialize":
        wanted = params.get("protocolVersion")
        reply(
            mid,
            {
                "protocolVersion": wanted if isinstance(wanted, str) and wanted else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return

    if method == "ping":
        reply(mid, {})
        return

    if method == "tools/list":
        reply(mid, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            text, is_error = call_tool(name, args)
        except Exception as e:  # never let a tool take the server down
            text, is_error = f"{name} failed: {e}", True
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        return

    fail(mid, -32601, f"method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(msg)
        except Exception as e:
            print(f"[mcp] {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
