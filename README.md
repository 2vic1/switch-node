# Switch Node

**Your machines, from your phone.**

Switch Node turns Telegram into a terminal for every computer you own. Send a
message, the job runs on the machine you named, the output comes back as a chat
reply. Screenshot any node with one command.

No cloud account. No port forwarding. No bot framework. Two Python files and the
standard library.

---

## The problem

You have a laptop, a Chromebook, an Android tablet and a phone. Work is spread
across all four. The moment you walk away from the laptop, everything on it is
unreachable — the agent mid-task, the build that is running, the browser tab you
left open. You end up carrying one machine around just to watch another one.

Existing answers all assume something you do not have: a public IP, a cloud
account, a paid agent runtime, or a desktop app you are chained to.

Switch Node assumes only that your machines can reach one address, and that you
have Telegram on your phone.

## Architecture

```
   phone (Telegram)
        │  /run laptop fix the auth bug
        ▼
   ┌─────────────┐        HTTP long-poll        ┌──────────────┐
   │    hub      │ ◀──────────────────────────▶ │    node      │  laptop
   │  bot token  │                              │  no token    │
   │  job queue  │ ◀──────────────────────────▶ │  runs work   │  chromebook
   │  registry   │                              │  no token    │  tablet
   └─────────────┘                              └──────────────┘
```

The hub holds the bot token. Nodes never see it — a compromised node cannot
message your Telegram, and a node behind NAT never needs an inbound port.

## Quick start

**1. Create a bot**

Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.

**2. Configure the hub**

```bash
cp config.example.json config.json
# put the token in config.json
python hub.py
```

The hub prints a node secret. Keep it.

**3. Start a node on each machine**

```bash
python node_agent.py --name laptop --hub http://<hub-ip>:8787 --secret <secret>
```

**4. Message your bot**

```
/start
/status
/run laptop uname -a
/shot laptop
```

The first chat to talk to the bot becomes the owner. Everyone else is refused.

## Commands

| Command | What it does |
|---|---|
| `/nodes` | who is online, with a freshness dot |
| `/status` | one line per node |
| `/run <node> <cmd>` | run a shell command, return output and exit code |
| `/agent <node> <prompt>` | hand the prompt to a coding agent CLI on that node |
| `/shot <node>` | screenshot the node's desktop |
| `/log <id>` | full output of a job |
| `/jobs` | last ten jobs |
| `/stop <node>` | cancel queued jobs |

## Design decisions

**Long-poll, not websockets.** A node polls the hub for up to 25 seconds. This
survives sleep, flaky wifi and NAT, and needs no reconnect logic. A node that
drops off is simply a node whose `last_seen` is old.

**The queue is the database.** SQLite, one row per job, `queued → running →
done`. Kill the hub mid-job and restart it: nothing is lost, nothing is re-run.

**Nodes are dumb.** They execute and report. All policy — who may send commands,
which node gets which job, what gets shown — lives in the hub. You can hand a
node to someone else and revoke it by rotating the secret.

**Agent CLIs are auto-detected.** `/agent` looks for `codebuff`, `opencode`,
`claude`, then `codex` on the node and uses whichever is installed, so the same
command works whether a node is a workstation with a paid model or a Chromebook
with a free one.

## Status

Working today: registration, long-poll dispatch, shell jobs, agent jobs,
screenshots, job log, cancellation, owner lockdown.

Next: Tailscale discovery so nodes find the hub without a hard-coded address;
per-node working directories; streaming output instead of one reply at the end.

## Files

```
hub.py                 bot + queue + HTTP api     (stdlib only)
node_agent.py          node daemon                (stdlib only)
config.example.json    template — copy to config.json
config.json            your token and secret      (gitignored)
data/hub.db            jobs and nodes             (created on first run)
```

`config.json` is gitignored on purpose: it holds the bot token. Only
`config.example.json` is tracked.

## License

MIT
