#!/data/data/com.termux/files/usr/bin/sh
# Switch Node — Termux bootstrap.
#
#   curl -fsSL http://<hub-ip>:8787/static/termux_setup.sh | sh -s <node-name>
#
# Installs python if missing, keeps Android from killing the process, pulls the
# agent from the hub, and restarts it whenever it drops.

HUB="${SWITCH_HUB:-http://192.168.1.125:8787}"
SECRET="${SWITCH_SECRET:-sn_3QpVx8mT2rLk9YdW4hJb6NcF1sZa}"
NAME="${1:-tablet}"

echo "[setup] node=$NAME hub=$HUB"

# A fresh Termux has stale package lists; without this the install below 404s
# on every mirror.
pkg update -y >/dev/null 2>&1 || true
command -v python >/dev/null 2>&1 || pkg install -y python

if ! command -v python >/dev/null 2>&1; then
    echo "[setup] python install failed — run 'pkg update && pkg install python' by hand"
    exit 1
fi

# Without this Android freezes the process once the screen is off.
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

cd "$HOME" || exit 1
curl -fsSL "$HUB/static/node_agent.py" -o node_agent.py || {
    echo "[setup] could not download the agent from $HUB"
    echo "[setup] is the tablet on the same WiFi as the hub?"
    exit 1
}

echo "[setup] agent downloaded — connecting"

while true; do
    python node_agent.py --name "$NAME" --hub "$HUB" --secret "$SECRET" || true
    echo "[setup] agent exited — restarting in 5s"
    sleep 5
done
