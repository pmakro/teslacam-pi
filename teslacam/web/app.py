"""Management UI (Flask). Runs unprivileged: privileged actions are requests the archiver picks up,
or go through the root-owned helper script (reboot)."""

from __future__ import annotations

import functools
import hmac
import io
import json
import re
import secrets
import socket
import subprocess
import time
import zipfile
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, urlsplit

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash

from .. import db, meter, tesla, uploader
from ..config import RUNTIME_SETTINGS, Config
from ..wifi import Nmcli, WifiError

EVENT_ID_RE = re.compile(r"^(SentryClips|SavedClips)/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
PAGE_SIZE = 48

REASONS = {
    "sentry_aware_object_detection": "Sentry · object detected",
    "sentry_aware_accel": "Sentry · vehicle bumped",
    "sentry_panic_accel": "Sentry · impact",
    "sentry_locked_handle_pulled": "Sentry · handle pulled",
    "user_interaction_honk": "Saved · horn",
    "user_interaction_dashcam_icon_tapped": "Saved · dashcam icon",
    "user_interaction_dashcam_panel_save": "Saved · dashcam panel",
    "user_interaction_dashcam_launcher_action_tapped": "Saved · launcher",
}

STATUS_FILTERS = {
    "pending": "uploaded = 0",
    "partial": "uploaded > 0 AND uploaded < nfiles",
    "uploaded": "uploaded = nfiles AND nfiles > 0",
    "failed": "failed > 0",
    "protected": "protected = 1",
}


def human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def ago(epoch) -> str:
    if not epoch:
        return "never"
    s = int(time.time() - float(epoch))
    if s < 60:
        return f"{max(s, 0)}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def pretty_reason(reason: str) -> str:
    if reason in REASONS:
        return REASONS[reason]
    for key, label in REASONS.items():
        if reason.startswith(key):
            return label
    return reason.replace("_", " ").capitalize() if reason else ""


def local_url(target: Optional[str], fallback: str) -> str:
    """Return `target` as a path on this site, or `fallback`, so redirects can't leave the UI.

    A same-host absolute URL (the Referer header) is first reduced to path + query. What is
    returned must then have no scheme and no host (the check CodeQL documents for
    py/url-redirection); backslashes are dropped because browsers read `/\\evil` as `//evil`.
    """
    if not target:
        return fallback
    target = target.replace("\\", "")
    parts = urlsplit(target)
    if parts.scheme in ("http", "https") and parts.netloc == request.host:
        target = parts.path + ("?" + parts.query if parts.query else "")
    parsed = urlsplit(target)
    if not parsed.scheme and not parsed.netloc and target.startswith("/"):
        return target
    return fallback


def create_app(cfg: Config, wifi_backend=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = cfg.get("web.secret_key") or secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      PERMANENT_SESSION_LIFETIME=30 * 86400, SEND_FILE_MAX_AGE_DEFAULT=3600)
    app.jinja_env.filters.update(
        bytes=human_bytes, ago=ago, reason=pretty_reason,
        dt=lambda iso: datetime.fromisoformat(iso).strftime("%a %d %b %Y, %H:%M:%S") if iso else "",
        epoch=lambda e: datetime.fromtimestamp(float(e)).strftime("%d %b %H:%M") if e else "—",
    )
    password_hash = cfg.get("web.password_hash")
    wifi = wifi_backend or Nmcli(cfg.get("network.interface"))
    hostname = socket.gethostname().split(".")[0].lower()
    own_hosts = {"localhost", "127.0.0.1", hostname, f"{hostname}.local", "teslacam.local",
                 str(cfg.get("network.setup_address"))}

    # --- plumbing -------------------------------------------------------------------------

    def conn():
        if "conn" not in g:
            g.conn = db.connect(cfg.db_path)
        return g.conn

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.before_request
    def _auth_and_csrf():
        # Captive portal: in setup mode every DNS name points at the Pi, so a phone's
        # connectivity check (generate_204, hotspot-detect.html, ...) lands here -> send it to /wifi.
        host = (request.host or "").split(":")[0].lower()
        if host not in own_hosts:
            state = db.status(conn(), "wifi") or {}
            if state.get("mode") == "setup":
                return redirect(f"http://{cfg.get('network.setup_address')}/wifi")
        if request.endpoint in ("login", "static"):
            return None
        if password_hash and not session.get("auth"):
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            token, expected = request.form.get("csrf", ""), session.get("csrf", "")
            if not expected or not hmac.compare_digest(token, expected):
                abort(400, "invalid form token, reload the page")
        return None

    @app.context_processor
    def _globals():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"csrf": session["csrf"], "auth_enabled": bool(password_hash),
                "nav": request.endpoint}

    def event_or_404(event_id: str):
        if not EVENT_ID_RE.match(event_id):
            abort(404)
        row = conn().execute("SELECT * FROM events WHERE id=? AND state='present'", (event_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def wake_uploader():
        db.request(conn(), "upload")

    # --- auth -----------------------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password_hash:
            return redirect(url_for("events"))
        if request.method == "POST":
            if check_password_hash(password_hash, request.form.get("password", "")):
                session.clear()
                session["auth"] = True
                session.permanent = True
                nxt = local_url(request.args.get("next"), "")
                # Repeated inline (CodeQL's documented form) so the scanner sees the check.
                if nxt and not urlparse(nxt).netloc and not urlparse(nxt).scheme:
                    return redirect(nxt)
                return redirect(url_for("events"))
            time.sleep(1)
            flash("Wrong password", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- events ---------------------------------------------------------------------------

    @app.get("/")
    def events():
        args = request.args
        where, params = ["e.state = 'present'"], []
        if args.get("type") in tesla.EVENT_TYPES:
            where.append("e.type = ?")
            params.append(args["type"])
        if args.get("date"):
            where.append("e.ts LIKE ?")
            params.append(args["date"][:10] + "%")
        if args.get("q"):
            where.append("(e.city LIKE ? OR e.reason LIKE ?)")
            params += [f"%{args['q']}%"] * 2
        having = STATUS_FILTERS.get(args.get("status", ""))
        page = max(int(args.get("page", 1) or 1), 1)
        max_attempts = int(cfg.get("upload.max_attempts"))
        sql = (
            "SELECT * FROM (SELECT e.*, COUNT(f.name) AS nfiles, COALESCE(SUM(f.size), 0) AS bytes,"
            " COALESCE(SUM(f.uploaded_at IS NOT NULL), 0) AS uploaded,"
            f" COALESCE(SUM(f.uploaded_at IS NULL AND f.attempts >= {max_attempts}), 0) AS failed"
            " FROM events e LEFT JOIN files f ON f.event_id = e.id"
            f" WHERE {' AND '.join(where)} GROUP BY e.id)"
            + (f" WHERE {having}" if having else "")
            + " ORDER BY ts DESC LIMIT ? OFFSET ?"
        )
        rows = conn().execute(sql, params + [PAGE_SIZE + 1, (page - 1) * PAGE_SIZE]).fetchall()
        return render_template("events.html", events=rows[:PAGE_SIZE], page=page,
                               has_next=len(rows) > PAGE_SIZE, args=args,
                               summary=_summary())

    def _summary():
        c = conn()
        totals = c.execute(
            "SELECT COUNT(DISTINCT e.id) AS events, COALESCE(SUM(f.size), 0) AS bytes"
            " FROM events e LEFT JOIN files f ON f.event_id = e.id WHERE e.state='present'"
        ).fetchone()
        return {
            "events": totals["events"], "bytes": totals["bytes"],
            "pending": uploader.pending_summary(c),
            "uploader": db.status(c, "uploader", {}) or {},
            "used": meter.used_bytes(c, cfg), "budget": meter.budget_bytes(c, cfg),
        }

    @app.post("/events/bulk")
    def events_bulk():
        ids = [i for i in request.form.getlist("ids") if EVENT_ID_RE.match(i)]
        action = request.form.get("action")
        c = conn()
        with db.transaction(c):
            for eid in ids:
                _apply_action(c, eid, action)
        if action in ("upload", "reupload"):
            wake_uploader()
        flash(f"{action.capitalize() if action else 'Nothing'}: {len(ids)} event(s)", "ok")
        back = local_url(request.referrer, "")
        # Repeated inline (CodeQL's documented form) so the scanner sees the check.
        if back and not urlparse(back).netloc and not urlparse(back).scheme:
            return redirect(back)
        return redirect(url_for("events"))

    def _apply_action(c, eid: str, action: Optional[str]) -> None:
        if action == "delete":
            c.execute("UPDATE events SET state='deleted', protected=0 WHERE id=? AND state='present'", (eid,))
        elif action == "protect":
            c.execute("UPDATE events SET protected=1 WHERE id=?", (eid,))
        elif action == "unprotect":
            c.execute("UPDATE events SET protected=0 WHERE id=?", (eid,))
        elif action == "upload":
            c.execute("UPDATE events SET upload_requested=1 WHERE id=?", (eid,))
            c.execute("UPDATE files SET attempts=0 WHERE event_id=? AND uploaded_at IS NULL", (eid,))
        elif action == "reupload":
            c.execute("UPDATE events SET upload_requested=1 WHERE id=?", (eid,))
            c.execute("UPDATE files SET uploaded_at=NULL, attempts=0, last_error='' WHERE event_id=?", (eid,))
        else:
            abort(400)

    @app.get("/event/<path:event_id>")
    def event(event_id):
        ev = event_or_404(event_id)
        files = conn().execute(
            "SELECT * FROM files WHERE event_id=? ORDER BY start_ts, camera, name", (event_id,)
        ).fetchall()
        segments = {}
        for f in files:
            if f["kind"] == "video":
                segments.setdefault(f["start_ts"], {})[f["camera"]] = url_for(
                    "media", event_id=event_id, name=f["name"])
        cams = [c for c in tesla.CAMERAS if any(c in s for s in segments.values())]
        player = {
            "segments": [{"start": k, "files": v} for k, v in sorted(segments.items())],
            "cameras": cams, "trigger": ev["ts"],
        }
        neighbours = {
            "newer": conn().execute("SELECT id FROM events WHERE state='present' AND ts > ? ORDER BY ts LIMIT 1",
                                    (ev["ts"],)).fetchone(),
            "older": conn().execute("SELECT id FROM events WHERE state='present' AND ts < ? ORDER BY ts DESC LIMIT 1",
                                    (ev["ts"],)).fetchone(),
        }
        return render_template("event.html", ev=ev, files=files, player=json.dumps(player).replace("</", "<\\/"),
                               has_thumb=any(f["name"] == "thumb.png" for f in files),
                               neighbours=neighbours, max_attempts=int(cfg.get("upload.max_attempts")))

    @app.post("/event/<path:event_id>/action")
    def event_action(event_id):
        event_or_404(event_id)
        action = request.form.get("action")
        c = conn()
        with db.transaction(c):
            _apply_action(c, event_id, action)
        if action in ("upload", "reupload"):
            wake_uploader()
        if action == "delete":
            flash("Event deleted", "ok")
            return redirect(url_for("events"))
        return redirect(url_for("event", event_id=event_id))

    @app.get("/media/<path:event_id>/<name>")
    def media(event_id, name):
        if not EVENT_ID_RE.match(event_id):
            abort(404)
        row = conn().execute(
            "SELECT 1 FROM files f JOIN events e ON e.id=f.event_id"
            " WHERE f.event_id=? AND f.name=? AND e.state='present'", (event_id, name)
        ).fetchone()
        path = cfg.archive_dir / event_id / name
        if row is None or not path.is_file():
            abort(404)
        return send_file(path, conditional=True, as_attachment=request.args.get("dl") == "1",
                         download_name=f"{event_id.split('/')[1]}_{name}" if request.args.get("dl") else None)

    @app.get("/event/<path:event_id>/download.zip")
    def download_zip(event_id):
        event_or_404(event_id)
        names = [r["name"] for r in conn().execute("SELECT name FROM files WHERE event_id=? ORDER BY name", (event_id,))]
        folder = cfg.archive_dir / event_id
        prefix = event_id.replace("/", "_")

        def generate():
            out = _ZipStream()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for name in names:
                    path = folder / name
                    if not path.is_file():
                        continue
                    info = zipfile.ZipInfo.from_file(path, f"{prefix}/{name}")
                    with z.open(info, "w", force_zip64=True) as dst, open(path, "rb") as src:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            dst.write(chunk)
                            yield out.pop()
                    yield out.pop()
            yield out.pop()

        return Response(generate(), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{prefix}.zip"'})

    # --- uploads --------------------------------------------------------------------------

    @app.get("/uploads")
    def uploads():
        c = conn()
        max_attempts = int(cfg.get("upload.max_attempts"))
        queue = c.execute(
            "SELECT e.id, e.type, e.ts, e.reason, e.upload_requested, COUNT(*) AS files, SUM(f.size) AS bytes,"
            " MIN(f.priority) AS priority, SUM(f.attempts >= ?) AS failed, MAX(f.last_error) AS last_error"
            " FROM files f JOIN events e ON e.id=f.event_id"
            " WHERE f.uploaded_at IS NULL AND e.state='present'"
            " GROUP BY e.id ORDER BY e.upload_requested DESC, MIN(f.priority), (e.type='SavedClips') DESC, e.ts DESC"
            " LIMIT 50", (max_attempts,)
        ).fetchall()
        recent = c.execute(
            "SELECT e.id, e.ts, e.reason, MAX(f.uploaded_at) AS at, SUM(f.size) AS bytes"
            " FROM files f JOIN events e ON e.id=f.event_id WHERE f.uploaded_at IS NOT NULL"
            " GROUP BY e.id ORDER BY at DESC LIMIT 15"
        ).fetchall()
        history = c.execute("SELECT * FROM usage ORDER BY period DESC LIMIT 6").fetchall()
        return render_template(
            "uploads.html", queue=queue, recent=recent, history=history,
            summary=_summary(), nas=db.status(c, "nas", {}) or {},
            last=db.status(c, "last_upload"), paused=db.setting(c, cfg, "upload.paused"),
            period=meter.current_period(c, cfg), destination=uploader.destination(cfg),
            heartbeat=db.status(c, "uploader_heartbeat"), enabled=cfg.get("upload.enabled"),
        )

    @app.post("/uploads/<action>")
    def uploads_action(action):
        c = conn()
        if action == "pause":
            db.set_setting(c, "upload.paused", True)
        elif action == "resume":
            db.set_setting(c, "upload.paused", False)
            wake_uploader()
        elif action == "retry-failed":
            c.execute("UPDATE files SET attempts=0 WHERE uploaded_at IS NULL")
            wake_uploader()
        else:
            abort(404)
        return redirect(url_for("uploads"))

    # --- system & settings ----------------------------------------------------------------

    @app.get("/system")
    def system():
        c = conn()
        return render_template(
            "system.html",
            car=db.status(c, "car_usage"), archive=db.status(c, "archive_usage"),
            car_attached=db.status(c, "car_attached"), car_present=db.status(c, "car_present"),
            archiver_error=db.status(c, "archiver_error"), last_cleanup=db.status(c, "last_cleanup"),
            beats={name: {"at": at, "alive": bool(at) and time.time() - at < 900}
                   for name, at in (("Archiver", db.status(c, "archiver_heartbeat")),
                                    ("Uploader", db.status(c, "uploader_heartbeat")))},
            pending_requests=[r["key"][8:] for r in c.execute("SELECT key FROM kv WHERE key LIKE 'request:%'")],
            wifi_state=db.status(c, "wifi"), wifi_recovery=db.status(c, "wifi_recovery"),
            logs=_logs(), mode=cfg.get("car.mode"), cleanup_pct=cfg.get("car.cleanup_free_pct"),
        )

    def _logs() -> str:
        cmd = ["journalctl", "--no-pager", "-o", "short", "-n", "150",
               "-u", "teslacam-archiver", "-u", "teslacam-uploader", "-u", "teslacam-web", "-u", "teslacam-gadget", "-u", "teslacam-netwatch"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return res.stdout or res.stderr
        except (OSError, subprocess.TimeoutExpired):
            return "Logs are only available on the Pi (journalctl not found)."

    @app.post("/system/<action>")
    def system_action(action):
        if action in ("eject", "attach", "cleanup"):
            db.request(conn(), action)
            flash({"eject": "Car drive will be ejected", "attach": "Car drive will be re-attached",
                   "cleanup": "Car drive cleanup requested"}[action] + " within a few seconds", "ok")
        elif action == "reboot":
            try:
                subprocess.Popen(["sudo", "-n", cfg.get("car.priv_helper"), "reboot"])
                flash("Rebooting…", "ok")
            except OSError as e:
                flash(f"Reboot failed: {e}", "error")
        else:
            abort(404)
        return redirect(url_for("system"))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        c = conn()
        if request.method == "POST":
            errors = []
            for key in RUNTIME_SETTINGS:
                if key == "upload.paused":
                    continue
                raw = request.form.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    db.set_setting(c, key, raw)
                except (ValueError, TypeError) as e:
                    errors.append(f"{key}: {e}")
            for e in errors:
                flash(e, "error")
            if not errors:
                flash("Settings saved", "ok")
            return redirect(url_for("settings"))
        values = {k: db.setting(c, cfg, k) for k in RUNTIME_SETTINGS}
        return render_template("settings.html", values=values, cfg=cfg)

    # --- wifi -----------------------------------------------------------------------------

    @app.get("/wifi")
    def wifi_page():
        c = conn()
        state = db.status(c, "wifi") or {}
        db.set_status(c, "wifi_ui_seen", time.time())  # keeps the setup hotspot up while in use
        error, known, live = "", [], {}
        try:
            known = wifi.known()
            live = wifi.status()
        except WifiError as e:
            error = str(e)
        return render_template("wifi.html", state=state, known=known, live=live, error=error,
                               scan=db.status(c, "wifi_scan") or {}, setup=state.get("mode") == "setup",
                               hostname=hostname)

    @app.post("/wifi/scan")
    def wifi_scan():
        state = db.status(conn(), "wifi") or {}
        if state.get("mode") == "setup":
            flash("Can't scan while the Pi runs its setup hotspot. The list below is from just before it started.", "error")
        else:
            try:
                db.set_status(conn(), "wifi_scan", {"at": time.time(), "networks": wifi.scan()})
            except WifiError as e:
                flash(f"Scan failed: {e}", "error")
        return redirect(url_for("wifi_page"))

    @app.post("/wifi/add")
    def wifi_add():
        ssid = (request.form.get("ssid_manual") or request.form.get("ssid") or "").strip()
        password = request.form.get("password", "")
        try:
            wifi.add(ssid, password)
            uuid = next((n["uuid"] for n in wifi.known() if n["ssid"] == ssid), None)
        except WifiError as e:
            flash(str(e), "error")
            return redirect(url_for("wifi_page"))
        state = db.status(conn(), "wifi") or {}
        if state.get("mode") == "setup" or request.form.get("connect"):
            db.request(conn(), "wifi_connect", {"uuid": uuid})
            flash(f"Saved “{ssid}”. The Pi is switching to it now: join “{ssid}” on your phone too, "
                  f"then open http://{hostname}.local", "ok")
        else:
            flash(f"Saved “{ssid}”. The Pi will use it whenever it is in range.", "ok")
        return redirect(url_for("wifi_page"))

    @app.post("/wifi/connect")
    def wifi_connect():
        uuid = request.form.get("uuid") or None
        db.request(conn(), "wifi_connect", {"uuid": uuid})
        flash("Switching network… if this page stops responding, join that network on your phone "
              f"and open http://{hostname}.local", "ok")
        return redirect(url_for("wifi_page"))

    @app.post("/wifi/remove")
    def wifi_remove():
        try:
            wifi.remove(request.form.get("uuid", ""))
            flash("Network removed", "ok")
        except WifiError as e:
            flash(str(e), "error")
        return redirect(url_for("wifi_page"))

    @app.get("/api/status")
    def api_status():
        c = conn()
        s = _summary()
        return {
            "uploader": s["uploader"], "pending": s["pending"], "used": s["used"], "budget": s["budget"],
            "nas": db.status(c, "nas"), "car_usage": db.status(c, "car_usage"),
            "archive_usage": db.status(c, "archive_usage"), "archiver_error": db.status(c, "archiver_error"),
        }

    return app


class _ZipStream(io.RawIOBase):
    """Write-only buffer that lets zipfile stream to an HTTP response."""

    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)

    def pop(self) -> bytes:
        data = bytes(self.buf)
        self.buf.clear()
        return data


def serve(cfg: Config) -> None:
    db.open_db(cfg).close()
    app = create_app(cfg)
    try:
        from waitress import serve as waitress_serve
    except ImportError:  # dev fallback
        app.run(host=cfg.get("web.host"), port=int(cfg.get("web.port")), threaded=True)
        return
    waitress_serve(app, host=cfg.get("web.host"), port=int(cfg.get("web.port")), threads=4)
