#!/usr/bin/env python3
"""Build a simulator flash image: merged firmware + preset NVS + avatar pack.

Partition offsets are read from the partition table inside the merged image
(0x8000), so the image stays correct when the partition layout changes.

--ota-host generates the NVS instead of --nvs: Wi-Fi = the simulator's open
"Emulator Host Bridge" network, ota_url = http://HOST:8003/xiaozhi/ota/ (the
test server). That needs the esp-idf-nvs-partition-gen package:

  uv run --with esp-idf-nvs-partition-gen python make_sim_image.py \
      ../../build/merged-binary.bin -o sim-avatar.bin --ota-host auto \
      --avatar ../../build-avatar/avatar.bin
"""

import argparse
import pathlib
import struct
import subprocess
import sys
import tempfile

from lan_ip import lan_ip

PARTITION_TABLE_OFFSET = 0x8000
ENTRY_MAGIC = 0x50AA
FLASH_SIZE = 8 * 1024 * 1024
SIM_WIFI_SSID = "Emulator Host Bridge"


def generate_nvs(host, size):
    with tempfile.TemporaryDirectory() as tmp:
        csv = pathlib.Path(tmp, "nvs.csv")
        csv.write_text("key,type,encoding,value\n"
                       "wifi,namespace,,\n"
                       f"ssid,data,string,{SIM_WIFI_SSID}\n"
                       "password,data,string,\n"
                       f"ota_url,data,string,http://{host}:8003/xiaozhi/ota/\n")
        out = pathlib.Path(tmp, "nvs.bin")
        subprocess.run([sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate", str(csv), str(out),
                        hex(size)], check=True, stdout=subprocess.DEVNULL)
        return out.read_bytes()


def read_partitions(image):
    parts = {}
    for i in range(0, 0xC00, 32):
        entry = image[PARTITION_TABLE_OFFSET + i:PARTITION_TABLE_OFFSET + i + 32]
        if len(entry) < 32:
            break
        magic, ptype, subtype, offset, size, label, _flags = struct.unpack("<HBBII16sI", entry)
        if magic != ENTRY_MAGIC:
            break
        parts[label.rstrip(b"\0").decode()] = (offset, size, ptype, subtype)
    return parts


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("firmware")
    parser.add_argument("-o", "--output", required=True)
    nvs = parser.add_mutually_exclusive_group()
    nvs.add_argument("--nvs", help="NVS partition image (Wi-Fi + ota_url)")
    nvs.add_argument("--ota-host", help="generate the NVS for this test-server host ('auto' = LAN address)")
    parser.add_argument("--avatar", help="avatar pack from build_avatar_pack.py")
    args = parser.parse_args()

    image = bytearray(pathlib.Path(args.firmware).read_bytes())
    parts = read_partitions(image)
    if not parts:
        sys.exit("no partition table found at 0x8000")
    for name, (offset, size, ptype, subtype) in parts.items():
        print(f"  {name:10s} 0x{offset:06x} {size // 1024:5d} KB  type {ptype} subtype 0x{subtype:02x}")

    payloads = {}
    if args.ota_host:
        host = lan_ip() if args.ota_host == "auto" else args.ota_host
        payloads["nvs"] = generate_nvs(host, parts["nvs"][1])
        print(f"generated nvs: ssid '{SIM_WIFI_SSID}', ota_url http://{host}:8003/xiaozhi/ota/")
    elif args.nvs:
        payloads["nvs"] = pathlib.Path(args.nvs).read_bytes()
    if args.avatar:
        payloads["avatar"] = pathlib.Path(args.avatar).read_bytes()

    for name, data in payloads.items():
        if name not in parts:
            sys.exit(f"firmware has no '{name}' partition")
        offset, size, _, _ = parts[name]
        path = name
        if len(data) > size:
            sys.exit(f"{path} ({len(data)} B) exceeds the {name} partition ({size} B)")
        end = offset + size
        if len(image) < end:
            image.extend(b"\xff" * (end - len(image)))
        # Erase the whole partition first so stale data never survives.
        image[offset:end] = b"\xff" * size
        image[offset:offset + len(data)] = data
        print(f"wrote {name}: {len(data)} B at 0x{offset:06x}")

    if len(image) > FLASH_SIZE:
        sys.exit(f"image is {len(image)} B, larger than the 8 MiB flash")
    pathlib.Path(args.output).write_bytes(image)
    print(f"{args.output}: {len(image)} B")


if __name__ == "__main__":
    main()
