"""Minimal XiaoZhi protocol test server (OTA, WebSocket, MQTT + UDP) for simulator testing.

Without MUSE_BRIDGE_URL it is not a real assistant (no ASR/LLM): it verifies
the device side of the protocol: handshake, mic uplink (decoded to WAV), TTS
downlink, display text, abort, and server->device MCP.

With MUSE_BRIDGE_URL (the Muse bridge, Muse-desktop-pet/server) it is an
all-voice Muse front end: push-to-talk speech is transcribed, repeated back
("收到，交给 Muse：…"), sent to Muse after a short undo window, and Muse's
replies (POST /muse-reply from the bridge) are spoken and put on the device's
reply card. ASR=whisper (default) transcribes locally with faster-whisper;
ASR=muse uses Muse's own dictation through the bridge (currently rejected by
the Muse service with HTTP 500).
"""
import asyncio, base64, json, os, re, secrets, struct, subprocess, sys, tempfile, time, uuid, wave
import urllib.parse
from aiohttp import ClientSession, ClientTimeout, web, WSMsgType
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
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
UDP_PORT = 8004
MQTT_CLIENTS = []  # asyncio StreamWriters of connected devices
MUSE_BRIDGE_URL = os.environ.get("MUSE_BRIDGE_URL", "").rstrip("/")
UNDO_SECONDS = float(os.environ.get("UNDO_SECONDS", "3"))
ASR = os.environ.get("ASR", "whisper")
# A local model directory (see README: download from ModelScope) or a
# faster-whisper model name, which is fetched from Hugging Face on first use.
_LOCAL_WHISPER = os.path.expanduser("~/.cache/whisper-models/faster-whisper-small")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL") or (_LOCAL_WHISPER if os.path.isdir(_LOCAL_WHISPER) else "small")
_whisper = None
UDP_SESSIONS = {}  # ssrc -> MqttSession
UDP_TRANSPORT = None
LAST_AVATAR = None  # last self.avatar.set_state arguments, replayed to devices that (re)connect


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
    """Conversation logic shared by the WebSocket and MQTT + UDP transports."""

    def __init__(self):
        self.id = uuid.uuid4().hex[:8]
        self.dec = opuslib.Decoder(16000, 1)
        self.pcm, self.listening, self.voiced, self.silent_ms = [], False, False, 0
        self.manual = False
        self.speaking_task = None
        self.pending = None  # {"text", "task"}: speech waiting out its undo window
        self.mcp_id = 0

    async def send_json(self, text):
        raise NotImplementedError

    async def send_audio(self, packet):
        raise NotImplementedError

    async def send(self, obj):
        obj.setdefault("session_id", self.id)
        log(">>", json.dumps(obj, ensure_ascii=False)[:300])
        await self.send_json(json.dumps(obj, ensure_ascii=False))

    async def mcp(self, method, params=None):
        self.mcp_id += 1
        await self.send({"type": "mcp", "payload": {"jsonrpc": "2.0", "id": self.mcp_id,
                                                     "method": method, "params": params or {}}})

    async def handle(self, data):
        """Device -> server control message (same JSON on both transports)."""
        t = data.get("type")
        if t == "listen" and data.get("state") == "start":
            self.listening, self.pcm, self.voiced, self.silent_ms = True, [], False, 0
            # Push-to-talk ("manual"): the user decides when the sentence ends.
            self.manual = data.get("mode") == "manual"
        elif t == "listen" and data.get("state") == "stop":
            if self.listening:
                self.listening = False
                await self.finish_utterance()
        elif t == "abort":
            if self.speaking_task and not self.speaking_task.done():
                self.speaking_task.cancel()
            if self.pending:
                self.pending["task"].cancel()
                text, self.pending = self.pending["text"], None
                log("UNDO", text)
                await self.send({"type": "alert", "status": "已撤回", "message": text, "emotion": "neutral"})

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
        if self.manual:
            return
        if self.voiced and self.silent_ms >= 900:
            self.listening = False
            asyncio.ensure_future(self.finish_utterance())
        elif not self.voiced and len(self.pcm) > 50:
            self.pcm = self.pcm[-10:]  # keep waiting; drop old silence

    async def finish_utterance(self):
        name = f"uplink-{self.id}-{int(time.time())}.wav"
        pcm = b"".join(self.pcm)
        with wave.open(name, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(pcm)
        log(f"UPLINK saved {name}: {len(self.pcm)} frames ({len(self.pcm)*60} ms), voiced={self.voiced}")
        voiced = self.voiced
        self.pcm, self.voiced, self.silent_ms = [], False, 0
        if MUSE_BRIDGE_URL:
            await self.to_muse(pcm, voiced)
            return
        await self.send({"type": "stt", "text": os.environ.get("STT", "（模拟识别）帮我在待办项目里加一个登录页面")})
        await self.send({"type": "llm", "emotion": "thinking", "text": "🤔"})
        self.speaking_task = asyncio.ensure_future(self.speak(REPLY))

    async def to_muse(self, pcm, voiced):
        if not voiced:
            self.speaking_task = asyncio.ensure_future(self.speak("没听清，请按住 OK 再说一遍"))
            return
        if ASR == "muse":
            result = await bridge_post("/transcribe", {"sample_rate": 16000,
                                                       "pcm16": base64.b64encode(pcm).decode()})
        else:
            result = await asyncio.get_running_loop().run_in_executor(None, whisper_transcribe, pcm)
        text = (result or {}).get("text", "").strip()
        log("MUSE TRANSCRIBE", json.dumps(result, ensure_ascii=False)[:200])
        if not text:
            reason = (result or {}).get("reason", "no_text")
            say = "没听清，请再说一遍" if reason == "no_text" else "Muse 暂时连不上，没有发出"
            self.speaking_task = asyncio.ensure_future(self.speak(say))
            return
        await self.send({"type": "stt", "text": text})
        self.speaking_task = asyncio.ensure_future(self.speak(f"收到，交给 Muse：{text}"))
        self.pending = {"text": text, "task": asyncio.ensure_future(self.send_after_undo(text))}

    async def send_after_undo(self, text):
        # The undo window starts after the confirmation has been spoken, so a
        # double-click during or shortly after it drops the message.
        try:
            if self.speaking_task:
                await asyncio.shield(self.speaking_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(UNDO_SECONDS)
        if self.pending and self.pending["text"] == text:
            self.pending = None
        result = await bridge_post("/send", {"text": text})
        log("MUSE SEND", json.dumps(result, ensure_ascii=False)[:200])
        if not result or result.get("status") != "accepted":
            await self.send({"type": "alert", "status": "没有发出",
                             "message": "Muse 没有确认收到", "emotion": "sad"})

    async def speak(self, text):
        packets = await asyncio.get_running_loop().run_in_executor(None, tts_packets, text)
        await self.send({"type": "tts", "state": "start"})
        await self.send({"type": "tts", "state": "sentence_start", "text": text})
        try:
            start = time.monotonic()
            for i, p in enumerate(packets):
                await self.send_audio(p)
                delay = start + (i + 1) * 0.06 * PACE - time.monotonic() - 0.3 * PACE
                if delay > 0:
                    await asyncio.sleep(delay)
            await asyncio.sleep(0.3)
            log(f"TTS sent {len(packets)} packets")
        except asyncio.CancelledError:
            log("TTS cancelled by abort")
        await self.send({"type": "tts", "state": "stop"})


class WsSession(Session):
    def __init__(self, ws):
        super().__init__()
        self.ws = ws

    async def send_json(self, text):
        await self.ws.send_str(text)

    async def send_audio(self, packet):
        await self.ws.send_bytes(packet)


class MqttSession(Session):
    """MQTT control channel; Opus audio over AES-128-CTR UDP (docs/mqtt-udp.md).

    The 16-byte packet header doubles as the CTR counter block:
    type(0x01) flags | payload_len | ssrc | timestamp | sequence.
    """

    def __init__(self, writer):
        super().__init__()
        self.writer = writer
        self.key = None
        self.nonce = None
        self.addr = None      # device UDP address, learnt from its first datagram
        self.remote_seq = 0
        self.local_seq = 0

    async def send_json(self, text):
        self.writer.write(mqtt_publish("devices/p2p/passport-sim", text))
        await self.writer.drain()

    def open_channel(self):
        self.id = uuid.uuid4().hex[:8]
        self.key = secrets.token_bytes(16)
        ssrc = secrets.token_bytes(4)
        self.nonce = bytes([1, 0, 0, 0]) + ssrc + bytes(8)
        self.addr, self.remote_seq, self.local_seq = None, 0, 0
        for k, v in list(UDP_SESSIONS.items()):
            if v is self:
                del UDP_SESSIONS[k]
        UDP_SESSIONS[ssrc] = self
        return {"server": HOST_IP, "port": UDP_PORT, "key": self.key.hex(), "nonce": self.nonce.hex()}

    def crypt(self, header, payload):
        c = Cipher(algorithms.AES(self.key), modes.CTR(header)).encryptor()
        return c.update(payload) + c.finalize()

    def on_datagram(self, data, addr):
        if len(data) < 16 or data[0] != 1:
            return
        length, seq = struct.unpack(">H", data[2:4])[0], struct.unpack(">I", data[12:16])[0]
        if len(data) != 16 + length or seq <= self.remote_seq:
            return
        self.remote_seq, self.addr = seq, addr
        self.on_audio(self.crypt(data[:16], data[16:]))

    async def send_audio(self, packet):
        if not self.addr or not UDP_TRANSPORT:
            return
        self.local_seq += 1
        header = bytearray(self.nonce)
        header[2:4] = struct.pack(">H", len(packet))
        header[8:12] = struct.pack(">I", int(time.monotonic() * 1000) & 0xFFFFFFFF)
        header[12:16] = struct.pack(">I", self.local_seq)
        UDP_TRANSPORT.sendto(bytes(header) + self.crypt(bytes(header), packet), self.addr)


class UdpAudio(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        session = UDP_SESSIONS.get(data[4:8]) if len(data) >= 8 else None
        if session:
            session.on_datagram(data, addr)


def whisper_transcribe(pcm):
    """Local ASR (faster-whisper, CPU). The model loads once, on first use."""
    global _whisper
    try:
        import numpy as np
        from faster_whisper import WhisperModel
        if _whisper is None:
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
        segments, _ = _whisper.transcribe(audio, language="zh", vad_filter=True,
                                          initial_prompt="以下是普通话的句子。")
        text = "".join(s.text for s in segments).strip()
        return {"status": "transcribed", "text": text} if text else {"status": "error", "reason": "no_text"}
    except Exception as error:
        log("ASR ERROR", repr(error)[:160])
        return {"status": "error", "reason": "asr_failed"}


async def bridge_post(path, body):
    try:
        async with ClientSession(timeout=ClientTimeout(total=40)) as http:
            async with http.post(MUSE_BRIDGE_URL + path, json=body) as resp:
                return await resp.json()
    except Exception as error:  # the bridge may be down; report, never crash the gateway
        log("BRIDGE ERROR", path, repr(error)[:160])
        return None


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    h = request.headers
    log("WS connect", {k: h.get(k) for k in ("Authorization", "Protocol-Version", "Device-Id", "Client-Id")})
    s = WsSession(ws)
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
            else:
                await s.handle(data)
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
    session = MqttSession(writer)
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
                if LAST_AVATAR:
                    asyncio.ensure_future(replay_avatar(writer))
            elif kind == 3:  # PUBLISH from device
                tlen = int.from_bytes(body[:2], "big")
                topic, rest = body[2:2 + tlen].decode(), body[2 + tlen:]
                qos = (first >> 1) & 3
                if qos:
                    writer.write(b"\x40\x02" + rest[:2])
                    rest = rest[2:]
                log("MQTT <<", topic, rest.decode(errors="replace")[:4000])
                try:
                    data = json.loads(rest)
                except ValueError:
                    data = {}
                if data.get("type") == "hello":
                    udp = session.open_channel()
                    await session.send({"type": "hello", "transport": "udp", "udp": udp,
                                        "audio_params": {"format": "opus", "sample_rate": 16000,
                                                         "channels": 1, "frame_duration": 60}})
                elif data.get("type") == "goodbye":
                    session.listening = False
                elif data.get("type") in ("listen", "abort"):
                    await session.handle(data)
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
        for k, v in list(UDP_SESSIONS.items()):
            if v is session:
                del UDP_SESSIONS[k]
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


async def replay_avatar(writer):
    """A device that just connected shows "syncing" until it gets a state; send
    the last known one instead of waiting for the bridge's next resync."""
    await asyncio.sleep(3)
    if writer not in MQTT_CLIENTS or not LAST_AVATAR:
        return
    call = {"jsonrpc": "2.0", "id": next(MCP_IDS), "method": "tools/call",
            "params": {"name": "self.avatar.set_state", "arguments": LAST_AVATAR}}
    writer.write(mqtt_publish("devices/p2p/passport-sim", json.dumps({"session_id": "", "type": "mcp", "payload": call})))
    await writer.drain()
    log("AVATAR replay >>", json.dumps(LAST_AVATAR, ensure_ascii=False))


async def avatar(request):
    """GET /avatar?state=working&subagents=2 -> MCP self.avatar.set_state on every channel."""
    args = {"state": request.query.get("state", "default")}
    if "subagents" in request.query:
        args["subagents"] = int(request.query["subagents"])
    if request.query.get("detail"):
        args["detail"] = request.query["detail"]
    global LAST_AVATAR
    if args["state"] not in ("level_up", "achievement"):  # milestones are one-shot
        LAST_AVATAR = args
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


async def broadcast(obj):
    """Send one JSON message to every connected device (WebSocket and MQTT)."""
    for s in list(DEVICES.values()):
        await s.send(dict(obj))
    text = json.dumps({"session_id": "", **obj}, ensure_ascii=False)
    for w in list(MQTT_CLIENTS):
        w.write(mqtt_publish("devices/p2p/passport-sim", text))
        await w.drain()


def plain_text(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)              # code blocks
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)           # links / images
    text = re.sub(r"[*_`#>|~-]+", " ", text)                        # markdown marks
    return re.sub(r"\s+", " ", text).strip()


def clip(text, limit):
    """First sentences that fit `limit` characters, else a hard cut with …"""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(p, 0, limit) for p in "。！？!?；;")
    return text[:cut + 1] if cut >= limit // 3 else text[:limit - 1] + "…"


def attachment_phrase(text, attachments):
    kinds = {}
    for a in attachments or []:
        kinds[a.get("kind", "file")] = kinds.get(a.get("kind", "file"), 0) + 1
    parts = []
    if "```" in text:
        parts.append("附带代码")
    names = {"image": "张图片", "audio": "段语音", "file": "个文件"}
    for kind, count in kinds.items():
        parts.append(f"{count}{names.get(kind, '个附件')}")
    return ("另外有" + "、".join(parts) + "，碰手机看") if parts else ""


async def muse_reply(request):
    """POST /muse-reply {text, attachments, createdAt} from the bridge: speak the
    summary and put it on the reply card. A real deployment would summarise
    with an LLM; here the first sentences are used."""
    body = await request.json()
    text = plain_text(body.get("text", ""))
    screen = clip(text, 30) or "Muse 回复了附件"
    extra = attachment_phrase(body.get("text", ""), body.get("attachments"))
    head = clip(text, 60)
    if head and extra and head[-1] not in "。！？!?…":
        head += "。"
    spoken = (head + extra) or "Muse 回复了"
    audio_url = f"http://{HOST_IP}:{PORT}/audio/notify.ogg?text=" + urllib.parse.quote(spoken)
    when = time.strftime("%H:%M", time.localtime((body.get("createdAt") or time.time() * 1000) / 1000))
    call = {"jsonrpc": "2.0", "id": next(MCP_IDS), "method": "tools/call",
            "params": {"name": "self.muse.set_reply",
                       "arguments": {"text": screen, "when": when, "audio_url": audio_url}}}
    await broadcast({"type": "mcp", "payload": call})
    await broadcast({"type": "notify", "audio_url": audio_url,
                     "subtitles": [{"start_ms": 0, "text": screen}]})
    log("MUSE REPLY >>", json.dumps({"screen": screen, "spoken": spoken}, ensure_ascii=False))
    return web.json_response({"id": call["id"], "screen": screen, "spoken": spoken,
                              "ws": len(DEVICES), "mqtt": len(MQTT_CLIENTS)})


async def on_startup(app):
    global UDP_TRANSPORT
    app["mqtt"] = await asyncio.start_server(mqtt_client, "0.0.0.0", MQTT_PORT)
    UDP_TRANSPORT, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        UdpAudio, local_addr=("0.0.0.0", UDP_PORT))
    log(f"mqtt listening on :{MQTT_PORT}, udp audio on :{UDP_PORT}, muse bridge: {MUSE_BRIDGE_URL or 'off'}")


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_post("/push-mqtt", push_mqtt)
app.router.add_get("/audio/notify.ogg", notify_audio)
app.router.add_get("/avatar", avatar)
app.router.add_post("/muse-reply", muse_reply)
app.router.add_route("*", "/xiaozhi/ota/", ota)
app.router.add_get("/xiaozhi/v1/", ws_handler)
app.router.add_post("/push", push)
web.run_app(app, host="0.0.0.0", port=PORT, print=lambda *_: log(f"listening on :{PORT}"))
