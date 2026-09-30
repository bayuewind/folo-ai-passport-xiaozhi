# Muse avatar for FoloToy AI Passport

Shows the Muse "Hatch" character in the centre of the XiaoZhi screen. The
server pushes the avatar state; the device only renders it.

> **Assets.** The videos are Muse's public avatar files (`https://muse.ai/avatars/*.mp4`).
> `build_avatar_pack.py` downloads them at build time and writes a local
> `build-avatar/avatar.bin` (git-ignored). Do not commit or redistribute the
> generated pack; replace the character with owned artwork before shipping.

## Build the avatar pack

```sh
uv run --with numpy --with pillow python scripts/muse_avatar/build_avatar_pack.py \
    -o build-avatar/avatar.bin --preview build-avatar/preview.png
```

Defaults: 96×96 px, 12 fps, all five variants, ~576 KB. The script self-tests
its encoder against a reference decoder, prints per-variant size and PSNR, and
fails if the pack exceeds the `avatar` partition (640 KB).

`--size` sets the frame buffer RAM (`size² × 2` bytes); measured on the
simulator the device still had ~56 KB minimum free heap after a voice session
at 96 px **without TLS**. Budget TLS (~30 KB) before raising the size.

## Partition layout

`main/boards/folotoy/ai-passport/partitions_muse.csv` shrinks both OTA slots
from `0x2f0000` to `0x2a0000` (the app is ~`0x240000`) and adds
`avatar` (data, subtype `0x40`) at `0x560000`, 640 KB. `assets` stays at
`0x600000`, so OTA keeps working. Without a valid pack the firmware logs a
warning and keeps the stock XiaoZhi emotion UI.

Flash the pack separately (the merged firmware image does not contain it):

```sh
esptool.py --chip esp32c3 write_flash 0x560000 build-avatar/avatar.bin
```

## State protocol (server → device)

Device MCP tool, available over WebSocket (in a conversation) and MQTT (idle):

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"self.avatar.set_state",
           "arguments":{"state":"working","subagents":2}}}
```

| `state` | Animation | Caption | Dot |
| --- | --- | --- | --- |
| `default` | idle loop | 空闲中 | green |
| `working` | working loop | 正在工作 (· N 个子智能体) | green |
| `making_something` | making loop | 正在制作 | green |
| `waiting` | still | 等你回应 | amber |
| `approval` | still | 需要你批准 | amber |
| `limited` | still | 用量已耗尽 | red |
| `syncing` | still | 等待同步 (boot default) | grey |
| `offline` | greyed still | 连接已中断 | red |
| `unknown` | greyed still | 状态未知 | grey |
| `level_up` / `achievement` | plays once, then returns to the last non-milestone state | 升级啦 / 达成成就 | green |

Optional `detail` (string) is the second line under the state, e.g.
`"已 3 分钟 · 2 个子任务"` or `"下个任务 18:00"`; it is clipped, never scrolled,
so keep it to about 12 characters. Without it a working state shows the
sub-agent count. `waiting`, `approval` and `limited` draw an amber ring
around the avatar.

Unknown `state` values return a JSON-RPC error. Captions are built in on the
device (MCP text is not covered by the server glyph push).

Suggested mapping from Muse `agent.status` activity codes (see the desktop pet
`state.cjs`):

| Muse `activity_code` | `state` |
| --- | --- |
| `online` | `default` |
| `responding`, `composing`, `working`, `compacting`, `waiting_for_subagents` | `working` |
| `making_something` | `making_something` |
| `waiting_for_user` | `waiting` |
| `needs_approval` | `approval` |
| `out_of_credits` | `limited` |
| disconnected, keep-alive failed, unknown code | `offline` / `unknown` — never `default` |

## Reply card and keys

`self.muse.set_reply {text, when, audio_url}` fills the second card ("最新回复"):
`text` is a short summary (3 lines of about 10 characters), `when` a time label,
`audio_url` an optional Ogg Opus clip that a click on OK replays.

| Key | With the avatar pack | Without (stock XiaoZhi) |
| --- | --- | --- |
| UP / DOWN click | previous / next card | volume ±10 |
| UP / DOWN hold | volume ±10 | volume ±10 |
| OK hold (≥ 400 ms) | talk (push-to-talk), release to send | — |
| OK click | replay the latest reply | start / stop the conversation |
| OK double click | stop speaking, undo a just-sent sentence, back to home | — |

The three keys share one ADC pin, so key combinations cannot be detected.

## Pack format

See the module docstring of `build_avatar_pack.py`: 256-colour palette per
variant, key frame + delta frames, token stream decoded by
`main/boards/folotoy/ai-passport/muse_avatar.cc` into one RGB565 buffer.
