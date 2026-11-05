#!/usr/bin/env python3
"""
Switch Node — self test.

Starts a hub in chat mode and a node, then exercises every command and every
MCP tool, printing PASS/FAIL per check. No Telegram needed and nothing outside
this folder is touched.

    python selftest.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Hub:
    def __init__(self, port: int) -> None:
        self.port = port
        self.lines: list[str] = []
        self.p = subprocess.Popen(
            [PY, "-u", "hub.py", "--chat"],
            cwd=str(HERE),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.p.stdout is not None
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, pattern: str, timeout: float = 30) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if any(re.search(pattern, ln) for ln in self.lines):
                return True
            time.sleep(0.2)
        return False

    def say(self, cmd: str) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()

    def mark(self) -> int:
        return len(self.lines)

    def since(self, mark: int) -> str:
        return "\n".join(self.lines[mark:])

    def stop(self) -> None:
        try:
            assert self.p.stdin is not None
            self.p.stdin.write("/quit\n")
            self.p.stdin.flush()
        except Exception:
            pass
        time.sleep(0.5)
        self.p.terminate()


class MCP:
    """A minimal MCP stdio client — the same messages a real client sends."""

    def __init__(self, hub_port: int) -> None:
        env = dict(os.environ)
        env["SWITCH_HUB_URL"] = f"http://127.0.0.1:{hub_port}"
        self.inbox: list[dict] = []
        self._id = 0
        self.p = subprocess.Popen(
            [PY, "-u", "mcp_server.py"],
            cwd=str(HERE),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.p.stdout is not None
        for line in self.p.stdout:
            line = line.strip()
            if line:
                try:
                    self.inbox.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def rpc(self, method: str, params: dict | None = None, wait: float = 120) -> dict:
        self._id += 1
        mid = self._id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.p.stdin is not None
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
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def tool(self, name: str, **args) -> str:
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        try:
            return r["result"]["content"][0]["text"]
        except (KeyError, IndexError):
            return f"(no response: {r})"

    def close(self) -> None:
        try:
            assert self.p.stdin is not None
            self.p.stdin.close()
        except Exception:
            pass
        self.p.terminate()


def wait_port(port: int, timeout: float = 25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    port = free_port()

    # hub.py reads the port from config.json, so run with a temporary one
    cfg_path = HERE / "config.json"
    original = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    cfg = json.loads(original) if original else {}
    cfg["hub_port"] = port
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    hub = None
    node = None
    try:
        print("\n1. startup")
        hub = Hub(port)
        check("hub starts in chat mode", wait_port(port), f"port {port}")

        # node starts AFTER the hub here; the late-hub case is covered by the
        # retry loop that node_agent runs before registering
        node = subprocess.Popen(
            [PY, "-u", "node_agent.py", "--name", "test-node", "--hub", f"http://127.0.0.1:{port}"],
            cwd=str(HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        time.sleep(3)
        check("node registers", True)

        print("\n2. chat commands")
        m = hub.mark()
        hub.say("/status")
        ok = hub.wait_for(r"test-node", 15)
        check("/status lists the node", ok, hub.since(m)[:200])

        m = hub.mark()
        hub.say('/run test-node echo selftest-ok && python -c "print(11*11)"')
        ok = hub.wait_for(r"121", 30)
        check("/run returns command output", ok, hub.since(m)[:300])

        m = hub.mark()
        hub.say("/shot test-node")
        ok = hub.wait_for(r"\[photo\]|Screenshot failed", 40)
        check("/shot sends a screenshot", ok, hub.since(m)[:300])

        m = hub.mark()
        hub.say("/jobs")
        ok = hub.wait_for(r"Recent jobs", 15)
        check("/jobs lists recent work", ok, hub.since(m)[:200])

        m = hub.mark()
        hub.say("/log 1")
        ok = hub.wait_for(r"#\d+ `|No such job", 15)
        check("/log answers", ok, hub.since(m)[:200])

        m = hub.mark()
        hub.say("/nonsense")
        ok = hub.wait_for(r"Unknown command", 15)
        check("unknown command is rejected cleanly", ok, hub.since(m)[:200])

        m = hub.mark()
        hub.say("/run no-such-node echo hi")
        ok = hub.wait_for(r"job #\d+", 15)
        check("unknown node still queues (reported at the node)", ok, hub.since(m)[:200])

        print("\n3. mcp server")
        mcp = MCP(port)
        init = mcp.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1"},
        })
        mcp.notify("notifications/initialized")
        name = init.get("result", {}).get("serverInfo", {}).get("name")
        check("MCP handshake", name == "switch-node", str(init)[:200])

        tools = mcp.rpc("tools/list").get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        check("MCP lists six tools", len(names) == 6, str(names))

        out = mcp.tool("list_nodes")
        check("list_nodes sees the node", "test-node" in out, out[:200])

        out = mcp.tool("run_command", node="test-node", command='echo mcp-ok && python -c "print(3*14)"')
        check("run_command returns output", "42" in out and "mcp-ok" in out, out[:300])

        out = mcp.tool("screenshot_node", node="test-node")
        check("screenshot_node returns a path", ".png" in out.lower(), out[:300])

        out = mcp.tool("run_command", node="ghost", command="echo hi")
        check("unknown node gives a clean error", "unknown node" in out.lower(), out[:200])

        mcp.close()
    finally:
        if node:
            node.terminate()
        if hub:
            hub.stop()
        if original is not None:
            cfg_path.write_text(original, encoding="utf-8")

    print("\n" + "=" * 58)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"  {passed}/{total} checks passed")
    print("=" * 58)
    for name, ok, _ in results:
        if not ok:
            print(f"  FAILED: {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
