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
| `approval` | still | 需要你的批准 | amber |
| `limited` | still | 用量已耗尽 | red |
| `syncing` | still | 等待同步 (boot default) | grey |
| `offline` | greyed still | 连接已中断 | red |
| `unknown` | greyed still | 状态未知 | grey |
| `level_up` / `achievement` | plays once, then returns to the last non-milestone state | 升级啦 / 达成成就 | green |

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

## Pack format

See the module docstring of `build_avatar_pack.py`: 256-colour palette per
variant, key frame + delta frames, token stream decoded by
`main/boards/folotoy/ai-passport/muse_avatar.cc` into one RGB565 buffer.
