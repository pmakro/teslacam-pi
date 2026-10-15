import re

import pytest

from teslacam import db, wifi
from teslacam.web.app import create_app


def test_split_terse():
    assert wifi.split_terse(r"My\:Net:72:WPA2") == ["My:Net", "72", "WPA2"]
    assert wifi.split_terse(r"back\\slash::") == ["back\\slash", "", ""]


def test_validate():
    wifi.validate("Car", "")
    wifi.validate("Car", "12345678")
    with pytest.raises(wifi.WifiError):
        wifi.validate("", "12345678")
    with pytest.raises(wifi.WifiError):
        wifi.validate("Car", "short")
    with pytest.raises(wifi.WifiError):
        wifi.validate("x" * 33, "")


class FakeBackend:
    def __init__(self):
        self.nets = [{"name": "Home", "uuid": "u1", "ssid": "Home", "priority": "0"}]
        self.added = []

    def known(self):
        return list(self.nets)

    def status(self):
        return {"connection": "Home", "ip": "192.168.1.20"}

    def scan(self):
        return [{"ssid": "Car LTE", "signal": 80, "secure": True}]

    def add(self, ssid, password):
        wifi.validate(ssid, password)
        self.added.append((ssid, password))
        self.nets.append({"name": ssid, "uuid": "u-" + ssid, "ssid": ssid, "priority": "10"})

    def remove(self, uuid):
        self.nets = [n for n in self.nets if n["uuid"] != uuid]


@pytest.fixture
def client(env):
    backend = FakeBackend()
    app = create_app(env.cfg, wifi_backend=backend)
    c = app.test_client()
    c.env, c.backend = env, backend
    return c


def token(c, path="/wifi"):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', c.get(path).get_data(as_text=True)).group(1)


def test_wifi_page_and_scan(client):
    html = client.get("/wifi").get_data(as_text=True)
    assert "Home" in html and "192.168.1.20" in html
    assert db.status(client.env.conn, "wifi_ui_seen")
    client.post("/wifi/scan", data={"csrf": token(client)})
    assert "Car LTE" in client.get("/wifi").get_data(as_text=True)


def test_add_in_client_mode_does_not_switch(client):
    client.post("/wifi/add", data={"ssid": "Car LTE", "password": "hunter2hunter2", "csrf": token(client)})
    assert client.backend.added == [("Car LTE", "hunter2hunter2")]
    assert db.kv_get(client.env.conn, "request:wifi_connect") is None


def test_add_in_setup_mode_switches(client):
    db.set_status(client.env.conn, "wifi", {"mode": "setup", "setup_ssid": "TeslaCam-Setup"})
    t = token(client)
    res = client.post("/wifi/add", data={"ssid_manual": "Car LTE", "password": "hunter2hunter2", "csrf": t},
                      follow_redirects=True)
    assert db.kv_get(client.env.conn, "request:wifi_connect") == {"uuid": "u-Car LTE"}
    assert "switching to it now" in res.get_data(as_text=True)


def test_add_rejects_bad_password(client):
    res = client.post("/wifi/add", data={"ssid": "Car", "password": "short", "csrf": token(client)},
                      follow_redirects=True)
    assert "8–63 characters" in res.get_data(as_text=True)
    assert client.backend.added == []


def test_remove_and_connect(client):
    t = token(client)
    client.post("/wifi/connect", data={"uuid": "u1", "csrf": t})
    assert db.kv_get(client.env.conn, "request:wifi_connect") == {"uuid": "u1"}
    client.post("/wifi/remove", data={"uuid": "u1", "csrf": t})
    assert client.backend.nets == []


def test_captive_portal_redirect_only_in_setup_mode(client):
    probe = "http://connectivitycheck.gstatic.com/generate_204"
    assert client.get(probe).status_code == 404  # normal mode: no hijacking
    db.set_status(client.env.conn, "wifi", {"mode": "setup"})
    res = client.get(probe)
    assert res.status_code == 302 and res.headers["Location"] == "http://10.42.0.1/wifi"
    assert client.get("/wifi").status_code == 200  # our own host is served normally


def test_backend_missing_shows_error(env, monkeypatch):
    def no_nmcli(*a, **k):
        raise FileNotFoundError("nmcli")

    monkeypatch.setattr(wifi.subprocess, "run", no_nmcli)
    app = create_app(env.cfg, wifi_backend=wifi.Nmcli("wlan0"))
    res = app.test_client().get("/wifi")
    assert res.status_code == 200
    assert "NetworkManager (nmcli) is not available" in res.get_data(as_text=True)
