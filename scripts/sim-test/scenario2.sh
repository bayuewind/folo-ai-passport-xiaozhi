#!/bin/bash
set -u; cd "$(dirname "$0")"; D=http://127.0.0.1:4199
wait_log_any() { for _ in $(seq 1 "$2"); do grep -q -- "$1" server.log && return 0; sleep 1; done; echo "TIMEOUT: $1"; return 1; }
curl -s $D/quit >/dev/null; for _ in $(seq 1 60); do nc -z 127.0.0.1 4199 2>/dev/null || break; sleep 0.5; done; : > server.log; rm -f uplink-*.wav shots/t-*.png
nohup node driver.mjs "${FW:-sim.bin}" > driver.out 2>&1 &
wait_log_any "OTA POST" 120; sleep 8
curl -s "$D/key?k=OK&ms=100" >/dev/null
wait_log_any '"type": "listen", "state": "start"' 90
echo "feed tone"; curl -s "$D/say?file=tone.wav"
wait_log_any "UPLINK saved" 120 && grep "UPLINK saved" server.log | tail -1
wait_log_any "sentence_start" 30; sleep 3
curl -s "$D/key?k=OK&ms=100" >/dev/null
wait_log_any '"type": "abort"' 60 && echo "abort ok"
sleep 12
echo "push alert"
curl -s -X POST localhost:8003/push -H 'content-type: application/json' \
  -d '{"type":"alert","status":"任务完成","message":"登录页面已完成，测试通过","emotion":"happy"}'; echo
for i in $(seq -w 1 12); do sleep 2; curl -s "$D/shot?name=t-$i" >/dev/null; done
echo done
