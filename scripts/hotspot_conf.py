#!/usr/bin/env python3
"""Write the hostapd config for the setup hotspot from the [network] section of config.toml.

Run as root by priv.sh, so everything from the config is checked before it reaches a
line-based config file or a command line.

Usage: hotspot_conf.py CONFIG_TOML OUTPUT_CONF
Prints: INTERFACE ADDRESS DHCP_START DHCP_END
"""

import ipaddress
import re
import sys
from typing import Tuple


def build(network: dict) -> Tuple[str, Tuple[str, str, str, str]]:
    """Return (hostapd config text, (interface, address, dhcp start, dhcp end))."""
    iface = network.get("interface", "wlan0")
    ssid = network.get("setup_ssid", "TeslaCam-Setup")
    password = network.get("setup_password", "")
    address = str(network.get("setup_address", "10.42.0.1"))

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface):
        raise ValueError("network.interface is not a valid interface name")
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid):
        raise ValueError("network.setup_ssid must be 1-32 bytes without control characters")
    is_hex_key = re.fullmatch(r"[0-9a-fA-F]{64}", password) is not None
    if not is_hex_key and (not 8 <= len(password) <= 63 or any(ord(c) < 32 for c in password)):
        raise ValueError("network.setup_password must be 8-63 characters without control characters")
    ip = ipaddress.IPv4Address(address)  # raises ValueError if malformed
    prefix = str(ip).rsplit(".", 1)[0]

    # Plain WPA2-PSK only. On the Pi Zero W, phones and laptops dropped out right after message
    # 3/4 of the handshake with a NetworkManager access point (which also offers WPA-PSK-SHA256).
    # They joined when hostapd ran with these settings.
    conf = "\n".join([
        f"interface={iface}",
        "driver=nl80211",
        f"ssid={ssid}",
        "hw_mode=g",
        "channel=6",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        f"wpa_psk={password}" if is_hex_key else f"wpa_passphrase={password}",
        "",
    ])
    return conf, (iface, str(ip), f"{prefix}.10", f"{prefix}.250")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    import tomllib  # Python 3.11+: the Pi has it, the build() tests don't need it

    with open(sys.argv[1], "rb") as f:
        network = tomllib.load(f).get("network", {})
    try:
        conf, settings = build(network)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(conf)
    print(" ".join(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
