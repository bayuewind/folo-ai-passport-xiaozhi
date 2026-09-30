# Browser-simulator test harness

Runs this firmware in the [FoloToy Passport Simulator](https://github.com/VOID001/FoloToy-Passport-Simulator)
(headless Chrome via Playwright) against a minimal XiaoZhi test server, and
drives it with scripted scenarios: key presses, microphone input, screenshots,
server pushes. No real device, no ASR/LLM: it verifies the device side.

> The simulator needs the audio-clock option from the fork
> `bayuewind/FoloToy-Passport-Simulator` (branch `feature/xiaozhi-sim-support`):
> XiaoZhi's real-time Opus encoding overwhelms the default wall-clock I2S pacing.
> The driver opens `http://127.0.0.1:4190/?audioClock=emulated` by default.

## Files

| File | Purpose |
| --- | --- |
| `server.py` | Test server: OTA, WebSocket protocol, minimal MQTT server (the firmware never subscribes, so pushes go straight down its connection), notify audio, `/push`, `/push-mqtt`, `/avatar`. TTS uses macOS `say`. |
| `driver.mjs` | Playwright driver on `127.0.0.1:4199`: `/shot`, `/key`, `/say` (feed a WAV at emulated speed), `/eval`, `/reload`, `/quit`. `HEADLESS=0` opens a visible window. |
| `make_sim_image.py` | Merged firmware + generated NVS (simulator Wi-Fi, `ota_url` → this server) + avatar pack, at offsets read from the image's partition table. |
| `lan_ip.py` | Host LAN address for the device (skips the fake-IP range of TUN VPNs). |
| `make_audio.sh` | Generates `say1.wav` (Chinese request) and `tone.wav` (1 kHz calibration). |
| `scenario.sh` | WebSocket: open channel, speech uplink, TTS + subtitles, OK abort, alert, MCP call. |
| `scenario2.sh` | 1 kHz tone through the mic path (checks sample rate), alert. |
| `scenario3.sh` | MQTT, device idle: `notify` voice + subtitles, `alert`. |
| `scenario_avatar.py` | Muse avatar: every state, animated vs still, correct variant, milestone return, invalid state, free heap. |
| `demo_states.sh` | Visible walk-through of all avatar states (optionally pausing the Muse bridge). |

Generated files (`*.bin`, `*.wav`, `*.log`, `shots/`) are git-ignored.

## Run

```sh
# Simulator (fork, branch feature/xiaozhi-sim-support)
cd FoloToy-Passport-Simulator && EMULATOR_NETWORK_ALLOW_PRIVATE=1 npm start

# Firmware + avatar pack, from the repository root
docker run --rm -v "$PWD":/project -w /project espressif/idf:v6.1 bash -c \
  "git config --global --add safe.directory '*' && python scripts/build.py folotoy/ai-passport --name ai-passport"
uv run --with numpy --with pillow python scripts/muse_avatar/build_avatar_pack.py -o build-avatar/avatar.bin

cd scripts/sim-test
npm ci && ./make_audio.sh
uv run --with esp-idf-nvs-partition-gen python make_sim_image.py ../../build/merged-binary.bin \
  -o sim-avatar.bin --ota-host auto --avatar ../../build-avatar/avatar.bin

# Test server: TRANSPORT=mqtt for idle pushes / avatar, websocket for conversations
TRANSPORT=mqtt PACE=6 DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \
  uv run --with aiohttp --with opuslib python -W ignore server.py

uv run --with pillow --with numpy python scenario_avatar.py sim-avatar.bin
FW=sim-avatar.bin ./scenario.sh      # with the server in TRANSPORT=websocket
```

`sim.bin` (the default `FW`) is the same image without `--avatar`.

## Notes

- Build with `espressif/idf:v6.1` (as upstream CI); the rolling `release-v6.1`
  image breaks `78/uart-uhci`.
- The simulator only shows XiaoZhi logs with a UART0 console. For debugging,
  build a local config with `CONFIG_ESP_CONSOLE_UART_DEFAULT=y` and pass
  `-c <file>` to `scripts/build.py`. **Simulator only:** UART0 TX is the
  backlight pin on the real board.
- Emulated time warps ahead while the CPU idles and falls behind under load,
  so animation speed differs from hardware; `PACE=6` slows TTS downlink to
  match heavy-load speed.
- The simulator's network bridge drops TCP connections idle for 120 s; the
  test server hands out MQTT keepalive 60 s for that reason.
- Screenshots are ~0.75× (179×239); `scenario_avatar.py` coordinates use that size.
- Wait for port 4199 to close before starting another driver (the scripts do).
