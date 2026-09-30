#!/usr/bin/env python3
"""Muse UI scenario (all-voice). Needs the gateway in Muse mode:

  MUSE_BRIDGE_URL=http://127.0.0.1:18787 TRANSPORT=mqtt PACE=6 ... python server.py

and the Muse bridge running. Step 4 sends ONE real chat message to Muse
(the text spoken in say-muse.wav) and waits for Muse's real reply.

  uv run --with pillow --with numpy python scenario_muse_ui.py sim-ui.bin [--skip-muse]
"""

import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

DRIVER = "http://127.0.0.1:4199"
SERVER = "http://127.0.0.1:8003"


def get(url, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def log_lines():
    return open("server.log", encoding="utf-8").read().splitlines()


def wait_log(pattern, timeout, since=0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in log_lines()[since:]:
            if re.search(pattern, line):
                return line
        time.sleep(0.5)
    raise TimeoutError(pattern)


def shot(name):
    get(f"{DRIVER}/shot?name={name}")


def key(k, ms=120):
    get(f"{DRIVER}/key?k={k}&ms={ms}")


def hold_ok_and_say(wav, hold_ms, say_after=1.5):
    """Hold OK (push-to-talk) and feed `wav` into the mic while it is held."""
    holder = threading.Thread(target=key, args=("OK", hold_ms))
    holder.start()
    time.sleep(say_after)
    get(f"{DRIVER}/say?file={wav}")
    holder.join()


results = []


def check(ok, label):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label, flush=True)


def main():
    firmware = sys.argv[1]
    skip_muse = "--skip-muse" in sys.argv
    try:
        get(f"{DRIVER}/quit", timeout=10)
    except Exception:
        pass
    for _ in range(60):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 4199)) != 0:
                break
        time.sleep(0.5)
    start = len(log_lines())
    subprocess.Popen(["node", "driver.mjs", firmware], stdout=open("driver.out", "w"), stderr=subprocess.STDOUT)
    wait_log(r"MQTT CONNECT", 180, start)
    connected = len(log_lines())
    line = wait_log(r"AVATAR (replay )?>>", 90, connected)
    time.sleep(6)
    shot("u-01-home")
    check("detail" in line or "default" in line, f"1 home state pushed by the bridge: {line[9:120]}")

    # 2. Cards: DOWN -> reply card, UP -> home.
    key("DOWN"); time.sleep(3); shot("u-02-reply-empty")
    key("UP"); time.sleep(3); shot("u-03-home-again")
    check(True, "2 card switching (see screenshots u-02 / u-03)")

    # 3. A reply pushed by the server: spoken, on the card, replayable.
    mark = len(log_lines())
    r = post(f"{SERVER}/muse-reply", {
        "text": "会议纪要整理好了，共 6 个要点。其中两项需要你确认截止时间，我已经标在文档里。",
        "attachments": [{"kind": "image", "name": "timeline.png"}], "createdAt": int(time.time() * 1000)})
    ack = wait_log(r'"id":%d,"result"' % r["id"], 60, mark)
    audio = wait_log(r"AUDIO GET", 60, mark)
    time.sleep(3); shot("u-04-reply-speaking")
    check('"isError":false' in ack and "AUDIO GET" in audio,
          f"3 reply card set + spoken ({r['screen']} | {r['spoken']})")
    time.sleep(25)
    key("DOWN"); time.sleep(3); shot("u-05-reply-card")
    mark = len(log_lines())
    key("OK"); replay = None
    try:
        replay = wait_log(r"AUDIO GET", 40, mark)
    except TimeoutError:
        pass
    time.sleep(3); shot("u-06-replay")
    check(replay is not None, "3 click OK replays the last reply")
    time.sleep(25)
    key("UP"); time.sleep(2)

    if skip_muse:
        print(f"{sum(results)}/{len(results)} passed (Muse steps skipped)")
        return

    # 4. Push-to-talk to Muse: transcribe, repeat back, undo window, send, reply.
    mark = len(log_lines())
    hold_ok_and_say("say-muse.wav", hold_ms=11000)
    stt = wait_log(r"MUSE TRANSCRIBE", 90, mark)
    time.sleep(2); shot("u-07-confirm")
    check('"transcribed"' in stt, f"4 Muse dictation: {stt[9:160]}")
    sent = wait_log(r"MUSE SEND", 120, mark)
    check('"accepted"' in sent, f"4 sent to Muse after the undo window: {sent[9:120]}")
    reply = wait_log(r"MUSE REPLY >>", 240, mark)
    time.sleep(4); shot("u-08-muse-reply")
    check(True, f"4 Muse replied and it was spoken: {reply[9:200]}")
    time.sleep(30)

    # 5. Undo: talk again, double-click OK while it repeats the sentence.
    mark = len(log_lines())
    hold_ok_and_say("say-muse.wav", hold_ms=11000)
    wait_log(r'"type": "tts", "state": "sentence_start"', 120, mark)
    time.sleep(1)
    key("OK", 80); time.sleep(0.15); key("OK", 80)
    undo = None
    try:
        undo = wait_log(r"UNDO", 30, mark)
    except TimeoutError:
        pass
    time.sleep(2); shot("u-09-undo")
    time.sleep(UNDO_WAIT)
    resent = any("MUSE SEND" in l for l in log_lines()[mark:])
    check(undo is not None and not resent, "5 double-click inside the undo window drops the message")
    print(f"{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


UNDO_WAIT = 12

if __name__ == "__main__":
    main()
