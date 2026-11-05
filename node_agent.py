#!/usr/bin/env python3
"""
Switch Node — node agent.

Runs on any machine you want to control. Polls the hub for work, executes it,
and ships the output back. It never holds the bot token, so a compromised node
cannot talk to your Telegram.

    python node_agent.py --name laptop --hub http://192.168.1.10:8787 --secret <secret>

Three job kinds
---------------
shell  — run a shell command, return stdout+stderr+exit code
agent  — hand a prompt to a coding agent CLI if one is installed
shot   — screenshot the desktop, return PNG
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

AGENT_CLIS = [
    ("codebuff", ["codebuff", "--print"]),
    ("opencode", ["opencode", "run"]),
    ("claude", ["claude", "-p"]),
    ("codex", ["codex", "exec"]),
]


def post(hub: str, path: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        hub.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run_shell(cmd: str, timeout: int) -> tuple[str, int]:
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
        )
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        return out.strip() or "(no output)", p.returncode
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s", 124
    except Exception as e:
        return f"error: {e}", 1


def pick_agent() -> tuple[str, list[str]] | None:
    for name, argv in AGENT_CLIS:
        if shutil.which(argv[0]):
            return name, argv
    return None


def run_agent(prompt: str, timeout: int) -> tuple[str, int]:
    found = pick_agent()
    if not found:
        return (
            "No coding agent CLI found on this node.\n"
            "Install one of: codebuff, opencode, claude, codex — or use /run instead.",
            127,
        )
    name, argv = found
    try:
        p = subprocess.run(argv + [prompt], capture_output=True, timeout=timeout, text=True, errors="replace")
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        return f"[{name}]\n" + (out.strip() or "(no output)"), p.returncode
    except subprocess.TimeoutExpired:
        return f"[{name}] timed out after {timeout}s", 124
    except Exception as e:
        return f"[{name}] error: {e}", 1


def screenshot(timeout: int) -> tuple[str, int, str | None]:
    """Return (message, exit_code, png_base64)."""
    system = platform.system()
    tmp = Path(tempfile.gettempdir()) / f"switch-shot-{int(time.time())}.png"

    if system == "Windows":
        ff = shutil.which("ffmpeg")
        if not ff:
            return "ffmpeg not found — install it to enable /shot", 127, None
        cmd = [ff, "-y", "-f", "gdigrab", "-i", "desktop", "-frames:v", "1", str(tmp)]
    elif system == "Darwin":
        cmd = ["screencapture", "-x", str(tmp)]
    else:
        if shutil.which("scrot"):
            cmd = ["scrot", "-o", str(tmp)]
        elif shutil.which("import"):
            cmd = ["import", "-window", "root", str(tmp)]
        elif shutil.which("grim"):
            cmd = ["grim", str(tmp)]
        else:
            return "no screenshot tool (scrot/import/grim)", 127, None

    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
        if not tmp.exists():
            return "screenshot produced no file", 1, None
        b64 = base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink(missing_ok=True)
        return "ok", 0, b64
    except Exception as e:
        return f"screenshot error: {e}", 1, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=socket.gethostname().split(".")[0].lower())
    ap.add_argument("--hub", default="http://127.0.0.1:8787")
    ap.add_argument("--secret", default=os.environ.get("SWITCH_NODE_SECRET", ""))
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    if not args.secret:
        # On the machine that also runs the hub, the secret is already in
        # config.json — no reason to make the operator copy it by hand.
        cfg_path = ROOT / "config.json"
        if cfg_path.exists():
            try:
                args.secret = json.loads(cfg_path.read_text(encoding="utf-8")).get("node_secret", "")
                if args.secret:
                    print("[node] using node_secret from config.json")
            except Exception:
                pass

    if not args.secret:
        print("[node] no secret — pass --secret <secret> (the hub prints it at startup)")
        sys.exit(1)

    info = {
        "name": args.name,
        "secret": args.secret,
        "host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
    }
    print(f"[node] {args.name} → {args.hub}")

    # The hub may not be up yet — a laptop that boots faster than the machine
    # running the hub, or a Chromebook that reconnects first. Wait for it
    # instead of dying, otherwise the node needs a manual restart every time.
    while True:
        try:
            post(args.hub, "/api/register", info)
            print("[node] registered")
            break
        except Exception as e:
            print(f"[node] hub not reachable yet ({e}) — retrying in 5s")
            time.sleep(5)

    idle = 0
    while True:
        try:
            resp = post(args.hub, "/api/poll", info, timeout=40)
            job = resp.get("job")
            if not job:
                idle += 1
                if idle % 20 == 0:
                    print(f"[node] idle {idle * 25}s")
                continue
            idle = 0
            jid, kind, payload = job["id"], job["kind"], job["payload"]
            print(f"[node] job #{jid} {kind}: {payload[:60]}")

            result = {"secret": args.secret, "job_id": jid, "exit_code": 0, "output": "", "png_b64": None}
            if kind == "shell":
                result["output"], result["exit_code"] = run_shell(payload, args.timeout)
            elif kind == "agent":
                result["output"], result["exit_code"] = run_agent(payload, args.timeout)
            elif kind == "shot":
                msg, code, b64 = screenshot(args.timeout)
                result["output"], result["exit_code"], result["png_b64"] = msg, code, b64
            else:
                result["output"], result["exit_code"] = f"unknown job kind {kind}", 2

            post(args.hub, "/api/result", result)
            print(f"[node] job #{jid} done exit={result['exit_code']}")

        except urllib.error.HTTPError as e:
            print("[node] hub said", e.code, e.read().decode(errors="ignore")[:120])
            time.sleep(5)
        except Exception as e:
            print("[node] offline:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
