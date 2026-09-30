"""Minimal XiaoZhi protocol test server (OTA + WebSocket) for simulator testing.

Not a real assistant: it has no ASR/LLM. It verifies the device side of the
protocol: handshake, mic uplink (decoded to WAV), TTS downlink, display text,
abort, and server->device MCP.
"""
import asyncio, json, os, struct, subprocess, sys, tempfile, time, uuid, wave
from aiohttp import web, WSMsgType
import opuslib

from lan_ip import lan_ip

HOST_IP = os.environ.get("HOST_IP") or lan_ip()
PORT = 8003
LOG = open("server.log", "a", buffering=1)
REPLY = os.environ.get("REPLY", "收到，我已经把任务交给 Codex，完成后会通知你。")
DEVICES = {}  # session_id -> ws
# Simulator runs ~6x slower than a real ESP32-C3; stretch downlink pacing to match.
PACE = float(os.environ.get("PACE", "1"))
TRANSPORT = os.environ.get("TRANSPORT", "websocket")
MQTT_PORT = 1883
MQTT_CLIENTS = []  # asyncio StreamWriters of connected devices


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    LOG.write(line + "\n")


def tts_packets(text, rate=16000):
    """Synthesize Chinese speech with macOS `say`, return raw 60 ms Opus packets."""
    with tempfile.TemporaryDirectory() as d:
        aiff, ogg = f"{d}/t.aiff", f"{d}/t.ogg"
        subprocess.run(["say", "-v", "Tingting", "-o", aiff, text], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", aiff, "-ar", str(rate),
                        "-ac", "1", "-c:a", "libopus", "-frame_duration", "60",
                        "-application", "voip", "-b:a", "24k", ogg], check=True)
        return ogg_packets(open(ogg, "rb").read())


def ogg_packets(data):
    packets, cur, pos = [], b"", 0
    while pos < len(data):
        assert data[pos:pos + 4] == b"OggS"
        nseg = data[pos + 26]
        segs = data[pos + 27:pos + 27 + nseg]
        body = pos + 27 + nseg
        for s in segs:
            cur += data[body:body + s]
            body += s
            if s < 255:
                packets.append(cur)
                cur = b""
        pos = body
    return [p for p in packets if not p.startswith((b"OpusHead", b"OpusTags"))]


async def ota(request):
    body = await request.text()
    try:
        info = json.loads(body) if body else {}
    except ValueError:
        info = {}
    log("OTA", request.method, "Device-Id=", request.headers.get("Device-Id"),
        "board=", json.dumps(info.get("board", {}), ensure_ascii=False)[:200])
    return web.json_response({
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": 480},
        "firmware": {"version": "2.5.0", "url": ""},
        "websocket": {"url": f"ws://{HOST_IP}:{PORT}/xiaozhi/v1/", "token": "test-token", "version": 1},
        **({"mqtt": {"endpoint": f"{HOST_IP}:{MQTT_PORT}", "client_id": "passport-sim",
                     "username": "test", "password": "test", "publish_topic": "device-server",
                     "keepalive": 60}} if TRANSPORT == "mqtt" else {}),
    })


class Session:
    def __init__(self, ws):
        self.ws, self.id = ws, uuid.uuid4().hex[:8]
        self.dec = opuslib.Decoder(16000, 1)
        self.pcm, self.listening, self.voiced, self.silent_ms = [], False, False, 0
        self.speaking_task = None
        self.mcp_id = 0

    async def send(self, obj):
        obj.setdefault("session_id", self.id)
        log(">>", json.dumps(obj, ensure_ascii=False)[:300])
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def mcp(self, method, params=None):
        self.mcp_id += 1
        await self.send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": self.mcp_id,
                                                     "method": method, "params": params or {}}})

    def on_audio(self, packet):
        self.frames = getattr(self, "frames", 0) + 1
        if self.frames % 25 == 0:
            log(f"uplink frames={self.frames} listening={self.listening}")
        if not self.listening:
            return
        pcm = self.dec.decode(packet, 960)
        self.pcm.append(pcm)
        samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
        rms = (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5
        if rms > 500:
            self.voiced, self.silent_ms = True, 0
        elif self.voiced:
            self.silent_ms += 60
        if self.voiced and self.silent_ms >= 900:
            self.listening = False
            asyncio.ensure_future(self.finish_utterance())
        elif not self.voiced and len(self.pcm) > 50:
            self.pcm = self.pcm[-10:]  # keep waiting; drop old silence

    async def finish_utterance(self):
        name = f"uplink-{self.id}-{int(time.time())}.wav"
        with wave.open(name, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"".join(self.pcm))
        log(f"UPLINK saved {name}: {len(self.pcm)} frames ({len(self.pcm)*60} ms), voiced={self.voiced}")
        self.pcm, self.voiced, self.silent_ms = [], False, 0
        await self.send({"type": "stt", "text": os.environ.get("STT", "（模拟识别）帮我在待办项目里加一个登录页面")})
        await self.send({"type": "llm", "emotion": "thinking", "text": "🤔"})
        self.speaking_task = asyncio.ensure_future(self.speak(REPLY))

    async def speak(self, text):
        packets = await asyncio.get_running_loop().run_in_executor(None, tts_packets, text)
        await self.send({"type": "tts", "state": "start"})
        await self.send({"type": "tts", "state": "sentence_start", "text": text})
        try:
            start = time.monotonic()
            for i, p in enumerate(packets):
                await self.ws.send_bytes(p)
                delay = start + (i + 1) * 0.06 * PACE - time.monotonic() - 0.3 * PACE
                if delay > 0:
                    await asyncio.sleep(delay)
            await asyncio.sleep(0.3)
            log(f"TTS sent {len(packets)} packets")
        except asyncio.CancelledError:
            log("TTS cancelled by abort")
        await self.send({"type": "tts", "state": "stop"})


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    h = request.headers
    log("WS connect", {k: h.get(k) for k in ("Authorization", "Protocol-Version", "Device-Id", "Client-Id")})
    s = Session(ws)
    DEVICES[s.id] = s
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                s.on_audio(msg.data)
                continue
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            log("<<", json.dumps(data, ensure_ascii=False)[:600])
            t = data.get("type")
            if t == "hello":
                await s.send({"type": "hello", "transport": "websocket",
                              "audio_params": {"format": "opus", "sample_rate": 16000,
                                               "channels": 1, "frame_duration": 60}})
                if data.get("features", {}).get("mcp"):
                    await s.mcp("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                               "clientInfo": {"name": "sim-test", "version": "0"}})
                    await s.mcp("tools/list")
            elif t == "listen" and data.get("state") == "start":
                s.listening, s.pcm = True, []
            elif t == "listen" and data.get("state") == "stop":
                if s.listening:
                    s.listening = False
                    await s.finish_utterance()
            elif t == "abort":
                if s.speaking_task and not s.speaking_task.done():
                    s.speaking_task.cancel()
    finally:
        DEVICES.pop(s.id, None)
        log("WS closed", s.id)
    return ws


async def push(request):
    """POST /push {json} -> send to all connected devices (test hook)."""
    body = await request.json()
    for s in list(DEVICES.values()):
        await s.send(dict(body))
    return web.json_response({"sent": len(DEVICES)})


def mqtt_packet(first_byte, body):
    n, length = len(body), b""
    while True:
        byte, n = n % 128, n // 128
        length += bytes([byte | (0x80 if n else 0)])
        if not n:
            return bytes([first_byte]) + length + body


def mqtt_publish(topic, payload):
    t = topic.encode()
    return mqtt_packet(0x30, len(t).to_bytes(2, "big") + t + payload.encode())


async def mqtt_client(reader, writer):
    """Just enough MQTT for the XiaoZhi firmware: it never subscribes, so the
    server pushes PUBLISH frames straight down the device's connection."""
    peer = writer.get_extra_info("peername")
    try:
        while True:
            first = (await reader.readexactly(1))[0]
            mult, length = 1, 0
            while True:
                b = (await reader.readexactly(1))[0]
                length += (b & 0x7F) * mult
                mult *= 128
                if not b & 0x80:
                    break
            body = await reader.readexactly(length)
            kind = first >> 4
            if kind == 1:  # CONNECT
                pos = 2 + int.from_bytes(body[:2], "big") + 4
                cid_len = int.from_bytes(body[pos:pos + 2], "big")
                log("MQTT CONNECT", peer, "client_id=", body[pos + 2:pos + 2 + cid_len].decode())
                writer.write(b"\x20\x02\x00\x00")
                MQTT_CLIENTS.append(writer)
            elif kind == 3:  # PUBLISH from device
                tlen = int.from_bytes(body[:2], "big")
                topic, rest = body[2:2 + tlen].decode(), body[2 + tlen:]
                qos = (first >> 1) & 3
                if qos:
                    writer.write(b"\x40\x02" + rest[:2])
                    rest = rest[2:]
                log("MQTT <<", topic, rest.decode(errors="replace")[:4000])
            elif kind == 8:  # SUBSCRIBE
                writer.write(mqtt_packet(0x90, body[:2] + b"\x00"))
            elif kind == 12:  # PINGREQ
                writer.write(b"\xd0\x00")
            elif kind == 14:  # DISCONNECT
                break
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        if writer in MQTT_CLIENTS:
            MQTT_CLIENTS.remove(writer)
        log("MQTT closed", peer)


async def push_mqtt(request):
    """POST /push-mqtt {json} -> PUBLISH to every connected MQTT device."""
    body = await request.text()
    for w in list(MQTT_CLIENTS):
        w.write(mqtt_publish("devices/p2p/passport-sim", body))
        await w.drain()
    log("MQTT >>", body[:300])
    return web.json_response({"sent": len(MQTT_CLIENTS)})


async def notify_audio(request):
    """GET /audio/notify.ogg?text=... -> mono Ogg Opus synthesized with `say`."""
    text = request.query.get("text", "任务完成")
    def build():
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["say", "-v", "Tingting", "-o", f"{d}/t.aiff", text], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", f"{d}/t.aiff", "-ar", "16000",
                            "-ac", "1", "-c:a", "libopus", "-b:a", "24k", "-frame_duration", "60",
                            f"{d}/t.ogg"], check=True)
            return open(f"{d}/t.ogg", "rb").read()
    data = await asyncio.get_running_loop().run_in_executor(None, build)
    log("AUDIO GET", request.path_qs, len(data), "bytes")
    return web.Response(body=data, content_type="audio/ogg")


MCP_IDS = iter(range(1000, 10**9))


async def avatar(request):
    """GET /avatar?state=working&subagents=2 -> MCP self.avatar.set_state on every channel."""
    args = {"state": request.query.get("state", "default")}
    if "subagents" in request.query:
        args["subagents"] = int(request.query["subagents"])
    call = {"jsonrpc": "2.0", "id": next(MCP_IDS), "method": "tools/call",
            "params": {"name": "self.avatar.set_state", "arguments": args}}
    for s in list(DEVICES.values()):
        await s.send({"type": "mcp", "payload": call})
    body = json.dumps({"session_id": "", "type": "mcp", "payload": call})
    for w in list(MQTT_CLIENTS):
        w.write(mqtt_publish("devices/p2p/passport-sim", body))
        await w.drain()
    log("AVATAR >>", json.dumps(args), f"ws={len(DEVICES)} mqtt={len(MQTT_CLIENTS)}")
    return web.json_response({"id": call["id"], "ws": len(DEVICES), "mqtt": len(MQTT_CLIENTS)})


async def on_startup(app):
    app["mqtt"] = await asyncio.start_server(mqtt_client, "0.0.0.0", MQTT_PORT)
    log(f"mqtt listening on :{MQTT_PORT}")


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_post("/push-mqtt", push_mqtt)
app.router.add_get("/audio/notify.ogg", notify_audio)
app.router.add_get("/avatar", avatar)
app.router.add_route("*", "/xiaozhi/ota/", ota)
app.router.add_get("/xiaozhi/v1/", ws_handler)
app.router.add_post("/push", push)
web.run_app(app, host="0.0.0.0", port=PORT, print=lambda *_: log(f"listening on :{PORT}"))
