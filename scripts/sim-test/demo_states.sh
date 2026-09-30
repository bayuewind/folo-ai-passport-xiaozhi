#!/bin/bash
# Visible demo: boot the Muse-avatar firmware in a Chrome window and walk
# through every avatar state. With MUSE_BRIDGE_COMPOSE set, first show the real
# Muse state pushed by the bridge, pause the bridge during the tour (its 60 s
# resync would override the demo states) and resume it afterwards.
#   FW=sim-avatar.bin MUSE_BRIDGE_COMPOSE=/path/to/Muse-desktop-pet/server/docker-compose.yml ./demo_states.sh
set -u; cd "$(dirname "$0")"
D=http://127.0.0.1:4199
BRIDGE_STATE_URL=${BRIDGE_STATE_URL:-http://127.0.0.1:18787/state}
COMPOSE_FILE_BRIDGE=${MUSE_BRIDGE_COMPOSE:-}
bridge() { [ -n "$COMPOSE_FILE_BRIDGE" ] && docker compose -f "$COMPOSE_FILE_BRIDGE" "$@" >/dev/null 2>&1; true; }
bridge_state() { curl -s "$BRIDGE_STATE_URL" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["muse"]["label"], "->", d["device"]["state"])'; }
say_step() { echo "$(date +%T) $*"; }
wait_log() { local start=$1; for _ in $(seq 1 "$3"); do tail -n +"$start" server.log | grep -q -- "$2" && return 0; sleep 1; done; return 1; }

curl -s $D/quit >/dev/null; for _ in $(seq 1 60); do nc -z 127.0.0.1 4199 2>/dev/null || break; sleep 0.5; done
touch server.log; start=$(( $(wc -l < server.log) + 1 ))
HEADLESS=0 nohup node driver.mjs "${FW:-sim-avatar.bin}" > driver.out 2>&1 &
say_step "booting firmware in the Chrome window"
wait_log "$start" "MQTT CONNECT" 180 && say_step "device online (Wi-Fi + MQTT)"
if [ -n "$COMPOSE_FILE_BRIDGE" ]; then
  say_step "waiting for the bridge to push the real Muse state (<=60s)"
  wait_log "$start" "AVATAR >>" 90 && say_step "real Muse state: $(bridge_state)"
  sleep 12
  say_step "pausing the bridge for the state tour"; bridge stop
fi

for s in "default" "working&subagents=2" "making_something" "waiting" "approval" "limited" "level_up" "achievement" "offline" "unknown" "syncing"; do
  say_step "state: ${s%%&*}"; curl -s "localhost:8003/avatar?state=$s" >/dev/null
  case "$s" in level_up|achievement) sleep 14 ;; *) sleep 10 ;; esac
done

if [ -n "$COMPOSE_FILE_BRIDGE" ]; then
  say_step "resuming the bridge"; bridge start
  start=$(( $(wc -l < server.log) + 1 ))
  wait_log "$start" "AVATAR >>" 90; sleep 3
  say_step "back to the real Muse state: $(bridge_state)"
fi
