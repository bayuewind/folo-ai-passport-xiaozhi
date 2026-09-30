#!/bin/bash
# Idle push over MQTT: notify (voice + subtitles) and alert, no conversation opened.
set -u; cd "$(dirname "$0")"; D=http://127.0.0.1:4199
HOST_IP=${HOST_IP:-$(python3 lan_ip.py)}
wait_log_any() { for _ in $(seq 1 "$2"); do grep -q -- "$1" server.log && return 0; sleep 1; done; echo "TIMEOUT: $1"; return 1; }
curl -s $D/quit >/dev/null; for _ in $(seq 1 60); do nc -z 127.0.0.1 4199 2>/dev/null || break; sleep 0.5; done; : > server.log; rm -f shots/m-*.png
nohup node driver.mjs "${FW:-sim.bin}" > driver.out 2>&1 &
wait_log_any "OTA POST" 120
wait_log_any "MQTT CONNECT" 120 && grep "MQTT CONNECT" server.log | tail -1
sleep 15; curl -s "$D/shot?name=m-00-idle" >/dev/null
TEXT="你的 Codex 任务已完成，登录页面已经写好，测试全部通过。"
Q=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$TEXT")
echo "push notify"
curl -s -X POST localhost:8003/push-mqtt -d "{\"type\":\"notify\",\"audio_url\":\"http://$HOST_IP:8003/audio/notify.ogg?text=$Q\",\"subtitles\":[{\"start_ms\":0,\"text\":\"Codex 任务已完成\"},{\"start_ms\":2500,\"text\":\"登录页面已写好，测试通过\"}]}"; echo
for i in $(seq -w 1 15); do sleep 3; curl -s "$D/shot?name=m-$i" >/dev/null; done
wait_log_any "AUDIO GET" 5 && grep "AUDIO GET" server.log | tail -1 | cut -c1-120
sleep 20
echo "push alert"
curl -s -X POST localhost:8003/push-mqtt -d '{"type":"alert","status":"需要确认","message":"是否推送到 GitHub？按 OK 确认","emotion":"thinking"}'; echo
sleep 12; curl -s "$D/shot?name=m-alert" >/dev/null
echo done
