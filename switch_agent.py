#!/usr/bin/env python3
"""An agent for a Switch room that can actually touch your machines.

Switch gives an agent a room to talk in. This file gives that agent hands: it
speaks Switch's MCP protocol, and every command it is asked for goes out to
`switch-node`'s hub, runs on the machine you named, and comes back as the
room's next message.

    person in the room:  @switch-node run hostname on windows-pc
    switch_agent.py:     Switch MCP -> hub -> windows-pc -> output
    person in the room:  victor

Standard library only, like the rest of switch-node. No SDK, no build step.

Environment:
    SWITCH_URL        where Switch core listens      (http://127.0.0.1:8000)
    SWITCH_AGENT_KEY  the api_key Switch printed when the agent was registered
    SWITCH_ROOM       the room id to sit in
    HUB_URL           the switch-node hub            (http://127.0.0.1:8787)
    HUB_SECRET        the hub's node secret
    DEFAULT_NODE      which machine to use when the ask names none
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SWITCH_URL = os.environ.get("SWITCH_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_KEY = os.environ.get("SWITCH_AGENT_KEY", "")
ROOM = os.environ.get("SWITCH_ROOM", "")
HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8787").rstrip("/")
HUB_SECRET = os.environ.get("HUB_SECRET", "")
DEFAULT_NODE = os.environ.get("DEFAULT_NODE", "")

SESSION: dict[str, str | None] = {"id": None}
_rpc_id = 0


# ── Switch's MCP endpoint ────────────────────────────────────────────────────
def rpc(method: str, params: dict | None = None, notify: bool = False) -> str:
    """One JSON-RPC call to Switch, returning the raw SSE body."""
    global _rpc_id
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        _rpc_id += 1
        payload["id"] = _rpc_id

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {AGENT_KEY}",
    }
    if SESSION["id"]:
        headers["Mcp-Session-Id"] = SESSION["id"]

    req = urllib.request.Request(
        SWITCH_URL + "/mcp/",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            SESSION["id"] = sid
        return r.read().decode()


def sse_json(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    return out


def tool(name: str, arguments: dict) -> dict:
    """Call one Switch tool and return its decoded JSON payload."""
    messages = sse_json(
        rpc("tools/call", {"name": name, "arguments": arguments})
    )
    for msg in messages:
        if "error" in msg:
            raise RuntimeError(msg["error"])
        for part in msg.get("result", {}).get("content", []):
            text = part.get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


# ── the hub ─────────────────────────────────────────────────────────────────
def hub(path: str, payload: dict | None = None) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        payload = {**payload, "secret": HUB_SECRET}
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        HUB_URL + path, data=body, headers=headers, method="POST" if body else "GET"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def run_on_node(node: str, command: str, timeout: float = 60.0) -> dict:
    job = hub("/api/queue", {"node": node, "kind": "shell", "payload": command})
    job_id = job["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.6)
        row = hub("/api/job", {"job_id": job_id}).get("job") or {}
        if row.get("status") in ("done", "failed"):
            return row
    return {"status": "timeout", "output": f"{node} did not answer within {timeout}s"}


# ── what a person's sentence asks for ───────────────────────────────────────
_ASK = re.compile(
    r"(?:^|\s)(?:run|execute|exec|shell)\s+(?P<rest>.+)$", re.IGNORECASE | re.DOTALL
)


def parse_ask(body: str, nodes: list[str]) -> tuple[str, str] | None:
    """Pull (node, command) out of a room message, or None if it is just chat.

    Two shapes are understood, because people write both:

        run hostname on windows-pc
        run windows-pc: hostname
    """
    match = _ASK.search(body)
    if not match:
        return None
    rest = match.group("rest").strip().strip("`").strip()

    for node in nodes:
        for pattern in (rf"^{re.escape(node)}\s*:\s*(.+)$", rf"^(.+?)\s+on\s+{re.escape(node)}\s*$"):
            hit = re.match(pattern, rest, re.IGNORECASE | re.DOTALL)
            if hit:
                return node, hit.group(1).strip().strip("`").strip()
    if DEFAULT_NODE:
        return DEFAULT_NODE, rest
    return None


# ── the loop ────────────────────────────────────────────────────────────────
def main() -> int:
    if not (AGENT_KEY and ROOM):
        print("SWITCH_AGENT_KEY and SWITCH_ROOM are required", file=sys.stderr)
        return 2

    rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "switch-node", "version": "1.0"},
        },
    )
    rpc("notifications/initialized", notify=True)
    joined = tool("connect_to_room", {"room_id": ROOM})
    print(f"[agent] in room: {joined.get('name', ROOM)}", flush=True)

    nodes = [n["name"] for n in hub("/api/nodes").get("nodes", [])]
    print(f"[agent] nodes reachable: {', '.join(nodes) or 'none'}", flush=True)

    seen: set[str] = set()
    while True:
        try:
            context = tool("read_context", {})
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] read failed: {exc}", flush=True)
            time.sleep(5)
            continue

        for thread in context.get("threads", []):
            root = thread.get("root", {})
            for message in [root, *thread.get("replies", [])]:
                mid = message.get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                if message.get("kind") != "message":
                    continue
                if not str(message.get("sender", "")).startswith("@switch-member"):
                    continue

                body = message.get("body", "")
                ask = parse_ask(body, nodes)
                if ask is None:
                    continue
                node, command = ask

                print(f"[agent] {node}$ {command}", flush=True)
                result = run_on_node(node, command)
                output = (result.get("output") or "").strip() or "(no output)"
                code = result.get("exit_code", result.get("status"))
                tool(
                    "post_message",
                    {"body": f"`{node}$ {command}` → exit {code}\n\n```\n{output[:3000]}\n```"},
                )

        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
