#!/usr/bin/env python3
"""Convert Muse "Hatch" avatar videos into an AI Passport avatar partition image.

The ESP32-C3 cannot decode H.264 and has no PSRAM, so frames are converted
offline into a compact stream that the firmware decodes into a single RGB565
frame buffer (see main/boards/folotoy/ai-passport/muse_avatar.cc):

  * every variant has its own 256-colour palette (RGB565);
  * frame 0 of a variant is a key frame, later frames only store pixels that
    changed visibly since the previously *displayed* frame;
  * a circular mask is baked in so the firmware never needs LVGL clipping.

File layout (little endian):

  Header   16 B  magic "MAVT", u16 version, u16 variant_count,
                 u16 width, u16 height, u16 background_rgb565, u16 reserved
  Variant  32 B  char name[16], u16 fps, u16 frame_count, u16 flags,
                 u16 reserved, u32 palette_offset, u32 frame_table_offset
  Palette        256 x u16 RGB565
  FrameTable     (frame_count + 1) x u32 absolute offsets into the file
  Frames         token stream, each token starts with a varint h:
                   h & 3 == 0: skip  h >> 2 pixels (keep displayed value)
                   h & 3 == 1: copy  h >> 2 palette indices that follow
                   h & 3 == 2: fill  h >> 2 pixels with the next index byte

The source videos are Muse's public avatar assets. They are downloaded at build
time and the generated image is a local artifact: do not commit it, and replace
the character with owned artwork before distributing firmware.
"""

import argparse
import io
import pathlib
import struct
import subprocess
import sys
import tempfile
import urllib.request

import numpy as np
from PIL import Image

MAGIC = b"MAVT"
VERSION = 1
FLAG_LOOP = 1

# name, source URL, loops (milestones play once and return to the previous state)
VARIANTS = [
    ("default", "https://muse.ai/avatars/hatch.mp4", True),
    ("working", "https://muse.ai/avatars/hatch_working.mp4", True),
    ("making_something", "https://muse.ai/avatars/hatch_making_something.mp4", True),
    ("level_up", "https://muse.ai/avatars/hatch_milestone_level_up.mp4", False),
    ("achievement", "https://muse.ai/avatars/hatch_milestone_achievement.mp4", False),
]

# Matches the avatar partition in main/boards/folotoy/ai-passport/partitions_muse.csv.
DEFAULT_PARTITION_SIZE = 0xA0000


def rgb565(rgb):
    rgb = np.asarray(rgb, dtype=np.uint16)
    return ((rgb[..., 0] >> 3) << 11) | ((rgb[..., 1] >> 2) << 5) | (rgb[..., 2] >> 3)


def rgb565_to_rgb(value):
    value = np.asarray(value, dtype=np.uint16)
    r = (value >> 11) & 0x1F
    g = (value >> 5) & 0x3F
    b = value & 0x1F
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=-1).astype(np.int16)


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def read_frames(path, size, fps):
    """Decode a video into a list of size x size RGB uint8 arrays at `fps`."""
    cmd = ["ffmpeg", "-loglevel", "error", "-i", str(path),
           "-vf", f"fps={fps},scale={size}:{size}:flags=lanczos",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    frame_bytes = size * size * 3
    count = len(raw) // frame_bytes
    return [np.frombuffer(raw[i * frame_bytes:(i + 1) * frame_bytes], np.uint8).reshape(size, size, 3)
            for i in range(count)]


def circle_alpha(size):
    """Anti-aliased disc coverage (0..1) with a 1 px soft edge."""
    c = (size - 1) / 2
    y, x = np.mgrid[0:size, 0:size]
    dist = np.sqrt((x - c) ** 2 + (y - c) ** 2)
    return np.clip(size / 2 - dist, 0, 1)[..., None]


def build_palette(frames, background):
    """256-entry palette: index 0 is the exact background, the rest from the frames."""
    montage = np.concatenate(frames, axis=0)
    quant = Image.fromarray(montage).quantize(colors=255, method=Image.Quantize.MEDIANCUT,
                                              dither=Image.Dither.NONE)
    colors = np.array(quant.getpalette()[:255 * 3], np.uint8).reshape(-1, 3)
    palette = np.vstack([np.array([background], np.uint8), colors])
    return rgb565(palette)


def nearest_indices(frame, palette_rgb):
    pixels = frame.reshape(-1, 1, 3).astype(np.int32)
    dist = ((pixels - palette_rgb[None, :, :].astype(np.int32)) ** 2).sum(axis=2)
    return dist.argmin(axis=1).astype(np.uint8)


def encode_frame(target, shown, palette_rgb, tolerance, key):
    """Encode `target` indices against the displayed indices `shown` (updated in place)."""
    n = len(target)
    if key:
        changed = np.ones(n, bool)
    else:
        diff = np.abs(palette_rgb[target] - palette_rgb[shown]).max(axis=1)
        changed = diff > tolerance
    out = bytearray()
    i = 0
    while i < n:
        if not changed[i]:
            j = i
            while j < n and not changed[j]:
                j += 1
            out += varint((j - i) << 2 | 0)
            i = j
            continue
        # Changed span: emit fills for runs of >= 4 identical indices, copies otherwise.
        j = i
        while j < n and changed[j]:
            j += 1
        k = i
        while k < j:
            run = k + 1
            while run < j and target[run] == target[k]:
                run += 1
            if run - k >= 4:
                out += varint((run - k) << 2 | 2) + bytes([target[k]])
                k = run
                continue
            lit = k
            while lit < j:
                run = lit + 1
                while run < j and target[run] == target[lit]:
                    run += 1
                if run - lit >= 4:
                    break
                lit = run
            out += varint((lit - k) << 2 | 1) + target[k:lit].tobytes()
            k = lit
        shown[i:j] = target[i:j]
        i = j
    return bytes(out)


def decode_frame(stream, shown):
    """Reference decoder mirroring the firmware (used by the self test)."""
    pos = pixel = 0
    while pixel < len(shown):
        h = shift = 0
        while True:
            byte = stream[pos]
            pos += 1
            h |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        kind, count = h & 3, h >> 2
        if kind == 1:
            shown[pixel:pixel + count] = np.frombuffer(stream[pos:pos + count], np.uint8)
            pos += count
        elif kind == 2:
            shown[pixel:pixel + count] = stream[pos]
            pos += 1
        elif kind != 0:
            raise ValueError(f"bad token kind {kind}")
        pixel += count
    if pixel != len(shown) or pos != len(stream):
        raise ValueError("frame stream length mismatch")


def fetch(url, cache_dir):
    path = cache_dir / url.rsplit("/", 1)[-1]
    if not path.exists():
        with urllib.request.urlopen(url, timeout=60) as resp:
            path.write_bytes(resp.read())
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-o", "--output", default="build/avatar.bin")
    parser.add_argument("--size", type=int, default=96, help="avatar edge in pixels (RAM = size^2 * 2)")
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--tolerance", type=int, default=10,
                        help="max per-channel change still treated as unchanged")
    parser.add_argument("--background", default="ffffff", help="screen colour outside the circle")
    parser.add_argument("--partition-size", type=lambda v: int(v, 0), default=DEFAULT_PARTITION_SIZE)
    parser.add_argument("--cache", default=None, help="directory holding/receiving the source MP4s")
    parser.add_argument("--preview", default=None, help="write a contact sheet PNG here")
    args = parser.parse_args()

    background = np.frombuffer(bytes.fromhex(args.background), np.uint8)
    cache = pathlib.Path(args.cache or tempfile.mkdtemp(prefix="muse-avatar-"))
    cache.mkdir(parents=True, exist_ok=True)
    alpha = circle_alpha(args.size)

    variants = []
    for name, url, loop in VARIANTS:
        # The firmware matches the fixed 16-byte field; longer names would be cut.
        assert len(name.encode()) <= 16, f"variant name too long: {name}"
        frames = read_frames(fetch(url, cache), args.size, args.fps)
        frames = [(f * alpha + background * (1 - alpha)).round().astype(np.uint8) for f in frames]
        palette = build_palette(frames, background)
        palette_rgb = rgb565_to_rgb(palette)
        shown = np.zeros(args.size * args.size, np.uint8)
        streams, sq_err = [], 0.0
        for i, frame in enumerate(frames):
            target = nearest_indices(frame, palette_rgb)
            streams.append(encode_frame(target, shown, palette_rgb, args.tolerance, key=(i == 0)))
            shown_rgb = palette_rgb[shown].reshape(args.size, args.size, 3)
            sq_err += float(((shown_rgb - frame.astype(np.int16)) ** 2).mean())
        # Self test: the reference decoder must reproduce exactly what the encoder displayed.
        check = np.zeros_like(shown)
        for s in streams:
            decode_frame(s, check)
        assert np.array_equal(check, shown), f"{name}: decoder mismatch"
        variants.append((name, loop, palette, streams, frames))
        print(f"{name:17s} {len(frames):3d} frames  {sum(map(len, streams)) / 1024:7.1f} KB  "
              f"key {len(streams[0]) / 1024:5.1f} KB  PSNR {10 * np.log10(255 ** 2 / (sq_err / len(frames))):.1f} dB")

    header_size, entry_size = 16, 32
    blob = bytearray(header_size + entry_size * len(variants))
    struct.pack_into("<4sHHHHHH", blob, 0, MAGIC, VERSION, len(variants), args.size, args.size,
                     int(rgb565(background)), 0)
    for index, (name, loop, palette, streams, _) in enumerate(variants):
        palette_offset = len(blob)
        blob += palette.astype("<u2").tobytes()
        table_offset = len(blob)
        blob += bytes(4 * (len(streams) + 1))
        offsets = []
        for s in streams:
            offsets.append(len(blob))
            blob += s
        offsets.append(len(blob))
        struct.pack_into(f"<{len(offsets)}I", blob, table_offset, *offsets)
        struct.pack_into("<16sHHHHII", blob, header_size + index * entry_size, name.encode(),
                         args.fps, len(streams), FLAG_LOOP if loop else 0, 0, palette_offset, table_offset)

    print(f"total {len(blob) / 1024:.1f} KB of {args.partition_size / 1024:.0f} KB partition, "
          f"frame buffer {args.size * args.size * 2 / 1024:.1f} KB RAM")
    if len(blob) > args.partition_size:
        sys.exit("avatar image does not fit the partition: lower --size or --fps, or raise --tolerance")

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    print(f"wrote {out}")

    if args.preview:
        cols = 8
        rows = []
        for name, _, palette, streams, _ in variants:
            shown = np.zeros(args.size * args.size, np.uint8)
            pics = []
            pal = rgb565_to_rgb(palette).astype(np.uint8)
            for i, s in enumerate(streams):
                decode_frame(s, shown)
                if i % max(1, len(streams) // cols) == 0 and len(pics) < cols:
                    pics.append(pal[shown].reshape(args.size, args.size, 3))
            pics += [np.full_like(pics[0], 128)] * (cols - len(pics))
            rows.append(np.concatenate(pics, axis=1))
        buf = io.BytesIO()
        Image.fromarray(np.concatenate(rows, axis=0)).save(buf, "PNG")
        pathlib.Path(args.preview).write_bytes(buf.getvalue())
        print(f"wrote {args.preview}")


if __name__ == "__main__":
    main()
