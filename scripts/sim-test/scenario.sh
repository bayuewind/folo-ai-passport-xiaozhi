#!/bin/bash
# End-to-end XiaoZhi-on-Passport scenario against the local test server.
set -u
cd "$(dirname "$0")"
D=http://127.0.0.1:4199
wait_log() {  # wait_log <pattern> <timeout_s>
  local start=$(wc -l < server.log)
  for _ in $(seq 1 "$2"); do
    tail -n +"$((start + 1))" server.log | grep -q -- "$1" && return 0
    sleep 1
  done
  echo "TIMEOUT waiting for: $1"; return 1
}
wait_log_any() { for _ in $(seq 1 "$2"); do grep -q -- "$1" server.log && return 0; sleep 1; done; echo "TIMEOUT: $1"; return 1; }
shot() { curl -s "$D/shot?name=$1" >/dev/null; echo "  [shot] $1"; }

curl -s $D/quit >/dev/null; for _ in $(seq 1 60); do nc -z 127.0.0.1 4199 2>/dev/null || break; sleep 0.5; done
: > server.log; rm -f uplink-*.wav shots/s-*.png
nohup node driver.mjs "${FW:-sim.bin}" > driver.out 2>&1 &
echo "1) boot + Wi-Fi + OTA"; wait_log_any "OTA POST" 120 && shot s-01-idle
sleep 8
echo "2) OK -> open channel, hello, MCP, listen"
curl -s "$D/key?k=OK&ms=100" >/dev/null
wait_log_any '"type": "listen", "state": "start"' 90 && shot s-02-listening
echo "3) speak into mic"; curl -s "$D/say?file=say1.wav"
wait_log_any "UPLINK saved" 120 && grep "UPLINK saved" server.log | tail -1
wait_log_any "sentence_start" 30; sleep 4; shot s-03-speaking
echo "4) OK while speaking -> abort"
curl -s "$D/key?k=OK&ms=100" >/dev/null
wait_log_any '"type": "abort"' 60 && grep '"type": "abort"' server.log | tail -1 | cut -c1-160
wait_log_any "TTS cancelled" 30 && echo "  server cancelled TTS stream"
sleep 10; shot s-04-after-abort
echo "5) server push while connected: alert + MCP tool call"
curl -s -X POST localhost:8003/push -H 'content-type: application/json' \
  -d '{"type":"alert","status":"任务完成","message":"登录页面已完成，测试通过","emotion":"happy"}'; echo
sleep 20; shot s-05-alert
curl -s -X POST localhost:8003/push -H 'content-type: application/json' \
  -d '{"type":"mcp","payload":{"jsonrpc":"2.0","id":99,"method":"tools/call","params":{"name":"self.audio_speaker.set_volume","arguments":{"volume":35}}}}'; echo
wait_log_any '"id": 99' 60 && grep '"id": 99' server.log | tail -1 | cut -c1-200
echo done
