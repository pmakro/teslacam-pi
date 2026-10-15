"""NetworkManager (nmcli) wrapper: link health, saved networks, scanning and the setup hotspot.

The `teslacam` user may control NetworkManager through a polkit rule installed by setup.sh,
so neither the web UI nor the network daemon needs sudo for this.
"""

from __future__ import annotations

import re
import subprocess
from typing import Dict, List, Optional, Tuple

HOTSPOT_CON = "teslacam-setup"
WIFI_TYPE = "802-11-wireless"


class WifiError(RuntimeError):
    pass


def validate(ssid: str, password: str) -> None:
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise WifiError("network name must be 1–32 bytes")
    if password and not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise WifiError("password must be 8–63 characters (or empty for an open network)")


def split_terse(line: str) -> List[str]:
    """Split one line of `nmcli -t` output (':' separated, '\\:' and '\\\\' escaped)."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


class Nmcli:
    def __init__(self, interface: str = "wlan0"):
        self.iface = interface

    def _run(self, *args: str, timeout: int = 45, check: bool = True) -> str:
        try:
            res = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise WifiError("NetworkManager (nmcli) is not available on this machine") from e
        except subprocess.TimeoutExpired as e:
            raise WifiError(f"nmcli {args[0]} timed out") from e
        if check and res.returncode != 0:
            raise WifiError(res.stderr.strip() or res.stdout.strip() or f"nmcli exited {res.returncode}")
        return res.stdout

    # --- health ----------------------------------------------------------------------------

    def gateway(self) -> Optional[str]:
        try:
            out = subprocess.run(["ip", "-4", "route", "show", "default", "dev", self.iface],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(r"default via (\S+)", out)
        return m.group(1) if m else None

    def probe(self) -> Tuple[str, str]:
        """('ok' | 'disconnected' | 'stuck', detail)."""
        gw = self.gateway()
        if not gw:
            return "disconnected", "not connected to any network"
        for _ in range(3):  # tolerate a dropped ping or two
            try:
                res = subprocess.run(["ping", "-n", "-c", "1", "-W", "3", "-I", self.iface, gw],
                                     capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if res.returncode == 0:
                m = re.search(r"time=([\d.]+)", res.stdout)
                return "ok", f"gateway {gw}" + (f", {m.group(1)} ms" if m else "")
        return "stuck", f"connected, but gateway {gw} is not answering"

    def status(self) -> Dict[str, str]:
        out = self._run("-t", "-f", "GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", self.iface, check=False)
        info = {"connection": "", "ip": ""}
        for line in out.splitlines():
            key, _, value = line.partition(":")
            if key == "GENERAL.CONNECTION":
                info["connection"] = value.strip()
            elif key.startswith("IP4.ADDRESS") and not info["ip"]:
                info["ip"] = value.split("/")[0].strip()
        return info

    # --- saved networks --------------------------------------------------------------------

    def known(self) -> List[Dict[str, str]]:
        out = self._run("-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY", "connection", "show")
        nets = []
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 4 or parts[2] != WIFI_TYPE or parts[0] == HOTSPOT_CON:
                continue
            ssid = self._run("-g", "802-11-wireless.ssid", "connection", "show", "uuid", parts[1], check=False).strip()
            nets.append({"name": parts[0], "uuid": parts[1], "ssid": ssid or parts[0], "priority": parts[3]})
        return nets

    def add(self, ssid: str, password: str) -> None:
        validate(ssid, password)
        for net in self.known():
            if net["ssid"] == ssid or net["name"] == ssid:
                self.remove(net["uuid"])
        # Note: nmcli takes the passphrase as an argument; on this single-user appliance that is
        # acceptable (it is stored in NetworkManager's root-only keyfile afterwards).
        args = ["connection", "add", "type", "wifi", "ifname", self.iface, "con-name", ssid,
                "ssid", ssid, "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        self._run(*args)

    def remove(self, uuid: str) -> None:
        self._run("connection", "delete", "uuid", uuid)

    def connect(self, uuid: Optional[str] = None) -> None:
        if uuid:
            self._run("connection", "up", "uuid", uuid, timeout=90)
        else:  # let NetworkManager pick the best saved network in range
            self._run("device", "connect", self.iface, timeout=90)

    def scan(self) -> List[Dict]:
        out = self._run("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                        "ifname", self.iface, "--rescan", "yes", timeout=60)
        best: Dict[str, Dict] = {}
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 3 or not parts[0]:
                continue
            ssid, signal, security = parts[0], int(parts[1] or 0), parts[2]
            if ssid not in best or signal > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": signal, "secure": security not in ("", "--")}
        return sorted(best.values(), key=lambda n: -n["signal"])

    # --- setup hotspot ---------------------------------------------------------------------

    def start_hotspot(self, ssid: str, password: str, address: str) -> None:
        validate(ssid, password)
        if not password:
            raise WifiError("the setup hotspot needs a password")
        settings = [
            "ssid", ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv4.addresses", f"{address}/24", "ipv6.method", "disabled",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password, "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable",
            "connection.autoconnect", "no",
        ]
        exists = self._run("-t", "-f", "NAME", "connection", "show", check=False).splitlines()
        if HOTSPOT_CON in exists:
            self._run("connection", "modify", HOTSPOT_CON, *settings)
        else:
            self._run("connection", "add", "type", "wifi", "ifname", self.iface, "con-name", HOTSPOT_CON, *settings)
        self._run("connection", "up", HOTSPOT_CON, timeout=60)

    def stop_hotspot(self) -> None:
        self._run("connection", "down", HOTSPOT_CON, check=False)

    def stations(self) -> int:
        """Devices currently joined to the setup hotspot."""
        try:
            out = subprocess.run(["iw", "dev", self.iface, "station", "dump"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return 0
        return out.count("Station ")
