# Switch Node — submission writeup

Text to paste into a submission form. Everything marked **[verified]** was run
on a Windows 11 machine and the output is quoted from the real run.

---

## Project title

Switch Node — your machines, from your phone

## Short description (one line)

Turns Telegram into a terminal for every computer you own: an agent in the chat
dispatches shell commands, coding-agent tasks and screenshots to a named machine
and brings the result back into the thread.

## Long description

I work across four devices — a Windows laptop, a 4 GB Chromebook, an Android
tablet and a phone. Work is spread across all of them, and the moment I walk
away from the laptop everything on it becomes unreachable: the build that is
running, the agent mid-task, the browser tab I left open. I ended up carrying
one machine around just to watch another one.

Every existing answer assumes something I do not have: a public IP, a cloud
account, a paid agent runtime, or a desktop app I am chained to.

Switch Node assumes two things: your machines can reach one address, and you
have Telegram on your phone.

**How it works.** A small hub holds the bot token, a SQLite job queue and a
registry of machines. Each machine runs a node agent that long-polls the hub for
work — so a node behind NAT never needs an inbound port, and a node never holds
the bot token, so a compromised machine cannot message my Telegram.

The hub exposes the queue over HTTP. `mcp_server.py` wraps that HTTP API as an
**MCP stdio server**, which means any MCP-capable agent can drive the fleet as
six tools:

| Tool | What it does |
|---|---|
| `list_nodes` | which machines are reachable, with a freshness dot |
| `run_command` | run a shell command on a named machine, return stdout + exit code |
| `agent_task` | hand a natural-language task to that machine's coding-agent CLI |
| `screenshot_node` | capture that machine's desktop, return the PNG path |
| `job_status` | collect a job that was still running |
| `cancel_queued` | drop that machine's pending queue |

Because it is plain MCP and not a bespoke plugin, the same file works in
Switch, Claude Code, Cursor, Codex and OpenCode with no changes.

**Design decisions.**

- *Long-poll, not websockets.* A node asks for work for up to 25 seconds. This
  survives sleep, flaky wifi and NAT, and needs no reconnect logic. A node that
  drops off is just a node whose `last_seen` is old.
- *The queue is the database.* One row per job, `queued → running → done`. Kill
  the hub mid-job and restart it: nothing is lost, nothing re-runs.
- *Nodes are dumb.* They execute and report. All policy — who may send commands,
  which node gets which job — lives in the hub. Revoke a machine by rotating the
  secret.
- *Stdlib only.* No `pip install`, no `node_modules`. Three Python files that run
  anywhere Python 3.9 runs, which matters on a 4 GB Chromebook.

**What was verified [verified].** On a Windows 11 hub plus one node:

```
list_nodes      → windows-pc · Windows 11 · online (24s ago)
run_command     → #5 on windows-pc · done · exit 0
                  switch-node mcp ok
                  42
screenshot_node → #6 on windows-pc · done · exit 0
                  …\switch-node\data\shots\6.png      (297 KB PNG, full desktop)
run_command     → unknown node 'toaster'. Known nodes: windows-pc
```

The last line matters as much as the others: an unknown node name returns a
clean error instead of hanging the agent.

## How this fits Switch

Switch connects an agent to the chat app the team already lives in. The agent
runs on the machine Switch Console runs on — the right default, but it means the
conversation is still tied to one computer.

Switch Node is the missing half: **the agent in the room can reach the other
machines.** Register `mcp_server.py` with the agent Switch runs and that agent
gains the six tools above, with no change to Switch itself. Ask in the room
*"what is the Chromebook doing?"* and the agent calls `run_command` on that node
and answers in the same thread.

Registering it is one entry in the agent's MCP config:

```json
{
  "mcpServers": {
    "switch-node": {
      "command": "python",
      "args": ["/absolute/path/to/switch-node/mcp_server.py"],
      "env": {
        "SWITCH_HUB_URL": "http://127.0.0.1:8787",
        "SWITCH_NODE_SECRET": "<the secret the hub printed>"
      }
    }
  }
}
```

## Honest status

- The hub, the node agent and the MCP server are written, tested and pushed.
- Switch Console 0.40.1 installs on Windows and starts, but creates no window,
  no child processes and no log on this machine, so the Switch room itself was
  not driven end to end here. The MCP integration was tested against the hub
  directly, which is the same path Switch would use.
- The hub has only been run on one machine so far. The second node (Chromebook
  over Crostini, or the tablet over Termux) is the next thing to bring up.

## Links

- Repository: https://github.com/2vic1/switch-node
- Bot: `@switch_node_bot`

## Tags

`agents` `mcp` `telegram` `remote-execution` `python` `stdlib` `self-hosted`
