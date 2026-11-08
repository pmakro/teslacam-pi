"""The setup hotspot: hostapd config generation and the call into the root helper."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from teslacam import wifi

_spec = importlib.util.spec_from_file_location(
    "hotspot_conf", Path(__file__).parent.parent / "scripts" / "hotspot_conf.py")
hotspot_conf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hotspot_conf)

NET = {"interface": "wlan0", "setup_ssid": "TeslaCam-Setup", "setup_password": "supersecret",
       "setup_address": "10.42.0.1"}


def test_config_offers_plain_wpa2_psk_only():
    conf, settings = hotspot_conf.build(NET)
    lines = conf.splitlines()
    assert "wpa_key_mgmt=WPA-PSK" in lines and "wpa=2" in lines and "rsn_pairwise=CCMP" in lines
    assert "ssid=TeslaCam-Setup" in lines and "wpa_passphrase=supersecret" in lines
    assert settings == ("wlan0", "10.42.0.1", "10.42.0.10", "10.42.0.250")


def test_config_uses_raw_key_for_64_hex_characters():
    conf, _ = hotspot_conf.build({**NET, "setup_password": "ab" * 32})
    assert f"wpa_psk={'ab' * 32}" in conf.splitlines() and "wpa_passphrase" not in conf


@pytest.mark.parametrize("override", [
    {"setup_password": ""},
    {"setup_password": "short"},
    {"setup_password": "x" * 64},
    {"setup_password": "goodpassword\ninterface=eth0"},
    {"setup_ssid": ""},
    {"setup_ssid": "x" * 33},
    {"setup_ssid": "Car\nwpa=0"},
    {"interface": "wlan0; reboot"},
    {"setup_address": "not-an-ip"},
    {"setup_address": "10.42.0.1; reboot"},
])
def test_config_rejects_bad_values(override):
    with pytest.raises(ValueError):
        hotspot_conf.build({**NET, **override})


def _record_runs(monkeypatch, returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(wifi.subprocess, "run", run)
    return calls


def test_hotspot_goes_through_the_root_helper(monkeypatch):
    calls = _record_runs(monkeypatch)
    nm = wifi.Nmcli("wlan0", helper="/opt/priv.sh")
    nm.start_hotspot()
    nm.stop_hotspot()
    assert calls == [["sudo", "-n", "/opt/priv.sh", "hotspot-start"],
                     ["sudo", "-n", "/opt/priv.sh", "hotspot-stop"]]


def test_hotspot_failure_is_reported(monkeypatch):
    _record_runs(monkeypatch, returncode=1, stderr="network.setup_password must be 8-63 characters")
    with pytest.raises(wifi.WifiError, match="8-63"):
        wifi.Nmcli("wlan0", helper="/opt/priv.sh").start_hotspot()


def test_hotspot_needs_the_helper():
    with pytest.raises(wifi.WifiError):
        wifi.Nmcli("wlan0").start_hotspot()
