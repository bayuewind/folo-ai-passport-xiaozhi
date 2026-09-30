#!/usr/bin/env python3
"""Avatar scenario: boot, then push every avatar state over MQTT (device idle)
and check the screen. Run with the test server in TRANSPORT=mqtt mode.

  uv run --with pillow --with numpy python scenario_avatar.py sim-avatar.bin

A running Muse bridge re-pushes the real state every 60 s and would override
the scripted states: set MUSE_BRIDGE_COMPOSE to its docker-compose.yml and the
bridge is paused for the run and resumed afterwards.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

import numpy as np
from PIL import Image

DRIVER = "http://127.0.0.1:4199"
SERVER = "http://127.0.0.1:8003"
# Avatar box in the driver's screenshots (the canvas is captured at ~0.75x,
# 179x239), measured from real shots.
AVATAR_BOX = (54, 70, 126, 142)
# Mean per-channel difference that separates two different variants from two
# frames of the same variant.
VARIANT_DELTA = 8.0

STATES = ["default", "working", "making_something", "waiting", "approval", "limited",
          "offline", "unknown", "syncing"]
ANIMATED = {"default", "working", "making_something"}


def get(url):
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read().decode()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode()


def log_text():
    return open("server.log", encoding="utf-8").read()


def wait_log(pattern, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = re.search(pattern, log_text())
        if match:
            return match
        time.sleep(1)
    raise TimeoutError(pattern)


def shot(name):
    get(f"{DRIVER}/shot?name={name}")
    return np.asarray(Image.open(f"shots/{name}.png").convert("RGB")).astype(int)


def avatar_delta(a, b):
    x0, y0, x1, y1 = AVATAR_BOX
    return float(np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).mean())


def mcp_reply(call_id, timeout=90):
    return wait_log(r'MQTT << device-server (\{.*"id":%d,.*\})' % call_id, timeout).group(1)


def push(state, subagents=None):
    query = f"state={state}" + (f"&subagents={subagents}" if subagents is not None else "")
    call_id = json.loads(get(f"{SERVER}/avatar?{query}"))["id"]
    return mcp_reply(call_id)


def bridge(action):
    compose = os.environ.get("MUSE_BRIDGE_COMPOSE")
    if compose:
        subprocess.run(["docker", "compose", "-f", compose, action], check=False, capture_output=True)
        return True
    return False


def main():
    firmware = sys.argv[1]
    if not bridge("stop"):
        try:
            get("http://127.0.0.1:18787/healthz")
            print("WARNING: a Muse bridge is running and will override states; set MUSE_BRIDGE_COMPOSE")
        except Exception:
            pass
    try:
        run(firmware)
    finally:
        bridge("start")


def run(firmware):
    try:
        get(f"{DRIVER}/quit")
    except Exception:
        pass
    # The old driver closes its browser before releasing the port; starting a
    # new one too early makes it die with EADDRINUSE.
    for _ in range(60):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 4199)) != 0:
                break
        time.sleep(0.5)
    else:
        sys.exit("driver port 4199 still busy")
    open("server.log", "w").close()
    subprocess.Popen(["node", "driver.mjs", firmware], stdout=open("driver.out", "w"),
                     stderr=subprocess.STDOUT)
    wait_log(r"MQTT CONNECT", 180)
    time.sleep(15)
    shot("a-00-boot")
    print("boot: syncing state shown before any server push")

    results = []
    regions = {}
    for i, state in enumerate(STATES, 1):
        subagents = 2 if state == "working" else None
        reply = push(state, subagents)
        ok = '"isError":false' in reply
        time.sleep(4)
        first = shot(f"a-{i:02d}-{state}")
        time.sleep(2)
        second = shot(f"a-{i:02d}-{state}-b")
        regions[state] = first
        delta = avatar_delta(first, second)
        moving = delta > 1.0
        expect = state in ANIMATED
        verdict = "PASS" if ok and moving == expect else "FAIL"
        results.append(verdict)
        print(f"{verdict} {state:17s} reply_ok={ok} avatar_delta={delta:5.2f} "
              f"animated={moving} expected={expect}")

    # Each animated state must show its own variant, not the default one.
    for state in ("working", "making_something"):
        delta = avatar_delta(regions["default"], regions[state])
        verdict = "PASS" if delta > VARIANT_DELTA else "FAIL"
        results.append(verdict)
        print(f"{verdict} {state} variant differs from default: delta={delta:5.2f}")

    # Milestone: must actually be on screen, then return to the previous state.
    push("working", 3)
    time.sleep(4)
    working = shot("a-19-working-ref")
    reply = push("level_up")
    showing = []
    for t in (1, 3):
        time.sleep(1 if t == 1 else 2)
        showing.append(avatar_delta(working, shot(f"a-20-level_up-{t}s")))
    # Emulated time runs ahead while the CPU idles, so the 6 s milestone can be
    # over by the 3 s sample; being on screen right after the push is the check,
    # returning to the previous state is checked next.
    ok_show = showing[0] > VARIANT_DELTA
    results.append("PASS" if ok_show else "FAIL")
    print(f"{results[-1]} level_up on screen after the push: deltas vs working at 1s/3s {showing}")
    wait_seconds, back = 3, False
    while wait_seconds < 120:
        time.sleep(3)
        wait_seconds += 3
        if avatar_delta(working, shot("a-21-after-level_up")) < VARIANT_DELTA:
            back = True
            break
    verdict = "PASS" if '"isError":false' in reply and back else "FAIL"
    results.append(verdict)
    print(f"{verdict} level_up returned to working after ~{wait_seconds}s wall")

    reply = push("dancing")
    verdict = "PASS" if '"error"' in reply and "Unknown avatar state" in reply else "FAIL"
    results.append(verdict)
    print(f"{verdict} invalid state rejected: {reply[:160]}")

    post(f"{SERVER}/push-mqtt", {"type": "mcp", "payload": {
        "jsonrpc": "2.0", "id": 777, "method": "tools/call",
        "params": {"name": "self.get_system_info", "arguments": {}}}})
    info = mcp_reply(777)
    heap = re.search(r'minimum_free_heap_size\\":\\"(\d+)', info)
    print(f"minimum_free_heap_size = {heap.group(1) if heap else '?'}")

    print(f"{results.count('PASS')}/{len(results)} passed")
    sys.exit(0 if all(r == "PASS" for r in results) else 1)


if __name__ == "__main__":
    main()
