#!/usr/bin/env python3
"""
Switch Node — demo.

Drives mcp_server.py over stdio exactly the way an MCP client (Switch's agent,
Claude Code, Cursor) would, and prints the transcript. Every line below is a
real round trip to the hub and out to a node.

    python demo.py

Needs the hub and at least one node running (start.cmd does both).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLOW = 0.5  # seconds between steps, so a screen recording reads well


def say(text: str = "") -> None:
    print(text, flush=True)


def rule(char: str = "─", n: int = 62) -> None:
    say(char * n)


class Client:
    def __init__(self) -> None:
        self.p = subprocess.Popen(
            [sys.executable, "-u", "mcp_server.py"],
            cwd=str(HERE),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.inbox: list[dict] = []
        threading.Thread(target=self._read, daemon=True).start()
        self._id = 0

    def _read(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if line:
                try:
                    self.inbox.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def rpc(self, method: str, params: dict | None = None, wait: float = 180) -> dict:
        self._id += 1
        mid = self._id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        end = time.time() + wait
        while time.time() < end:
            for m in self.inbox:
                if m.get("id") == mid:
                    return m
            time.sleep(0.1)
        return {}

    def notify(self, method: str) -> None:
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def tool(self, name: str, **args) -> str:
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        try:
            return r["result"]["content"][0]["text"]
        except (KeyError, IndexError):
            return "(no response)"

    def close(self) -> None:
        self.p.stdin.close()
        self.p.terminate()


def main() -> None:
    rule("═")
    say("  SWITCH NODE  —  one chat, every machine")
    say("  an MCP client driving a real hub and a real node")
    rule("═")
    say()

    c = Client()
    init = c.rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "demo", "version": "1"},
    })
    c.notify("notifications/initialized")
    info = init.get("result", {}).get("serverInfo", {})
    say(f"  connected to MCP server: {info.get('name')} v{info.get('version')}")
    time.sleep(SLOW)

    tools = c.rpc("tools/list").get("result", {}).get("tools", [])
    say(f"  tools offered: {', '.join(t['name'] for t in tools)}")
    time.sleep(SLOW)
    say()

    rule()
    say("  1. which machines can I reach?")
    rule()
    say(c.tool("list_nodes"))
    time.sleep(SLOW + 0.5)

    say()
    rule()
    say("  2. run something on a machine that is not this one")
    rule()
    say("  → run_command(node=<the node>, command='echo ... ')")
    say()
    say(c.tool("run_command", node="windows-pc",
               command='echo hello from the node && python -c "print(6*7)"'))
    time.sleep(SLOW + 0.5)

    say()
    rule()
    say("  3. look at that machine's screen")
    rule()
    say(c.tool("screenshot_node", node="windows-pc"))
    time.sleep(SLOW + 0.5)

    say()
    rule()
    say("  4. a machine that does not exist")
    rule()
    say(c.tool("run_command", node="toaster", command="echo hi"))
    time.sleep(SLOW)

    say()
    rule("═")
    say("  done — every line above came back from a real node")
    rule("═")
    c.close()


if __name__ == "__main__":
    main()
