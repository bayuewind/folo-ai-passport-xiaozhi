#!/usr/bin/env python3
"""Convert a PNG into a native LVGL RGB565A8 binary image.

The ESP32-C3 has no PSRAM, so decoding a PNG at runtime can fail with an out of
memory error. A native binary image is read straight from flash and needs no
decoding.

Usage: make_avatar_bin.py <input.png> <output.bin> [--width 80]
"""
import argparse
import struct

from PIL import Image


def convert(src, dst, width):
    image = Image.open(src).convert('RGBA')
    height = round(image.height * width / image.width)
    image = image.resize((width, height), Image.LANCZOS)
    color = bytearray()
    alpha = bytearray()
    for r, g, b, a in image.getdata():
        color += struct.pack('<H', ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
        alpha.append(a)
    # LVGL image header: magic 0x19, cf RGB565A8 (0x14), flags, w, h, stride, reserved
    header = struct.pack('<BBHHHHH', 0x19, 0x14, 0, width, height, width * 2, 0)
    with open(dst, 'wb') as f:
        f.write(header + color + alpha)
    print(f'{dst}: {width}x{height}, {12 + len(color) + len(alpha)} bytes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('input')
    parser.add_argument('output')
    parser.add_argument('--width', type=int, default=80)
    args = parser.parse_args()
    convert(args.input, args.output, args.width)
