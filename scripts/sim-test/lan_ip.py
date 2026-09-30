#!/usr/bin/env python3
"""Print the host's private LAN IPv4 address.

The simulated device reaches host services through the simulator's network
bridge, so it needs an address the host really owns. The default-route trick
is wrong behind a TUN VPN (it returns the VPN's fake-IP range, e.g.
198.18.0.1), so pick a private address from the interfaces instead.
"""
import ipaddress
import re
import subprocess


def lan_ip():
    try:
        text = subprocess.run(["ifconfig"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        text = subprocess.run(["ip", "-4", "addr"], capture_output=True, text=True).stdout
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", text):
        address = ipaddress.ip_address(match.group(1))
        # 198.18.0.0/15 (benchmark range used by TUN VPNs) is not "private" here.
        if address.is_private and not address.is_loopback and address not in ipaddress.ip_network("198.18.0.0/15"):
            return str(address)
    raise SystemExit("no private LAN IPv4 address found; set HOST_IP explicitly")


if __name__ == "__main__":
    print(lan_ip())
