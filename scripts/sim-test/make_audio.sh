#!/bin/bash
# Generate the test audio used by the scenarios (macOS `say` + ffmpeg):
#   say1.wav  0.3 s silence + Chinese request + 2.5 s silence (scenario.sh)
#   tone.wav  0.5 s silence + 1.0 s 1 kHz tone + 2.5 s silence (scenario2.sh)
set -eu; cd "$(dirname "$0")"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
say -v Tingting -o "$tmp/q.aiff" "帮我在待办项目里加一个登录页面"
ffmpeg -y -loglevel error -f lavfi -t 0.3 -i anullsrc=r=16000:cl=mono -i "$tmp/q.aiff" \
  -f lavfi -t 2.5 -i anullsrc=r=16000:cl=mono \
  -filter_complex "[1:a]aresample=16000,aformat=channel_layouts=mono[v];[0:a][v][2:a]concat=n=3:v=0:a=1" \
  -ar 16000 -ac 1 -c:a pcm_s16le say1.wav
ffmpeg -y -loglevel error -f lavfi -t 0.5 -i anullsrc=r=16000:cl=mono \
  -f lavfi -t 1.0 -i "sine=frequency=1000:sample_rate=16000" -f lavfi -t 2.5 -i anullsrc=r=16000:cl=mono \
  -filter_complex "[1:a]volume=0.5[t];[0:a][t][2:a]concat=n=3:v=0:a=1" -ar 16000 -ac 1 -c:a pcm_s16le tone.wav
echo "wrote say1.wav tone.wav"
