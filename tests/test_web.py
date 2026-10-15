import io
import re
import zipfile
from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from teslacam import archiver, db
from teslacam.web.app import create_app

from .conftest import make_event

EID = "SentryClips/2026-10-01_14-03-22"


@pytest.fixture
def client(env):
    make_event(env.teslacam, "SentryClips", datetime(2026, 10, 1, 14, 3, 22), minutes=2)
    archiver.scan(env.conn, env.cfg, env.teslacam)
    app = create_app(env.cfg)
    app.testing = True
    c = app.test_client()
    c.env = env
    return c


def csrf(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def test_pages_render(client):
    for path in ("/", "/uploads", "/system", "/settings", f"/event/{EID}"):
        res = client.get(path)
        assert res.status_code == 200, path
    assert "Sentry · object detected" in client.get("/").get_data(as_text=True)


def test_filters(client):
    assert EID in client.get("/?type=SentryClips&status=pending").get_data(as_text=True)
    assert EID not in client.get("/?type=SavedClips").get_data(as_text=True)
    assert EID not in client.get("/?status=uploaded").get_data(as_text=True)
    assert EID in client.get("/?date=2026-10-01&q=Athens").get_data(as_text=True)


def test_media_supports_range(client):
    name = "2026-10-01_14-02-22-front.mp4"
    res = client.get(f"/media/{EID}/{name}", headers={"Range": "bytes=0-9"})
    assert res.status_code == 206 and len(res.data) == 10
    assert client.get(f"/media/{EID}/../../teslacam.db").status_code == 404
    assert client.get(f"/media/{EID}/nope.mp4").status_code == 404


def test_zip_download(client):
    res = client.get(f"/event/{EID}/download.zip")
    z = zipfile.ZipFile(io.BytesIO(res.data))
    assert len(z.namelist()) == 2 * 4 + 2
    assert z.read(z.namelist()[0])


def test_post_requires_csrf(client):
    assert client.post(f"/event/{EID}/action", data={"action": "protect"}).status_code == 400


def test_actions(client):
    token = csrf(client)
    env = client.env
    client.post(f"/event/{EID}/action", data={"action": "protect", "csrf": token})
    assert env.conn.execute("SELECT protected FROM events").fetchone()[0] == 1
    client.post(f"/event/{EID}/action", data={"action": "upload", "csrf": token})
    assert env.conn.execute("SELECT upload_requested FROM events").fetchone()[0] == 1
    assert db.kv_get(env.conn, "request:upload")
    client.post("/events/bulk", data={"ids": [EID], "action": "delete", "csrf": token})
    assert env.conn.execute("SELECT state FROM events").fetchone()[0] == "deleted"
    assert client.get(f"/event/{EID}").status_code == 404


def test_settings_and_requests(client):
    token = csrf(client, "/settings")
    client.post("/settings", data={"budget.monthly_gb": "7.5", "retention.days": "3", "csrf": token})
    env = client.env
    assert db.setting(env.conn, env.cfg, "budget.monthly_gb") == 7.5
    assert db.setting(env.conn, env.cfg, "retention.days") == 3
    res = client.post("/settings", data={"budget.billing_day": "40", "csrf": token}, follow_redirects=True)
    assert "between 1 and 28" in res.get_data(as_text=True)
    client.post("/system/cleanup", data={"csrf": token})
    assert db.take_request(env.conn, "cleanup")
    client.post("/uploads/pause", data={"csrf": token})
    assert db.setting(env.conn, env.cfg, "upload.paused") is True


def test_login(env):
    env.cfg.data["web"]["password_hash"] = generate_password_hash("secret", method="pbkdf2:sha256:1000")
    app = create_app(env.cfg)
    c = app.test_client()
    assert c.get("/").status_code == 302
    page = c.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    assert c.post("/login", data={"password": "nope", "csrf": token}).status_code == 200
    assert c.post("/login", data={"password": "secret", "csrf": token}).status_code == 302
    assert c.get("/").status_code == 200
