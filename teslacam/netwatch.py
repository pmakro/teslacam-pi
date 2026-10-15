"""Network daemon: wifi watchdog + fallback setup hotspot.

Client mode (normal): the Pi is joined to the car hotspot (or any saved network).
  * Connected but the gateway stops answering for `stuck_minutes` -> the watchdog recovers the
    wifi: radio toggle, then driver reload (the Zero W's brcmfmac chip occasionally wedges),
    with backoff up to an hour. It never reboots: that would pull the drive from the car.
  * Not connected to anything for `fallback_minutes` (immediately if no network is saved)
    -> switch to setup mode.

Setup mode: the Pi runs its own network (`setup_ssid`, 10.42.0.1) and the UI's Wifi page lets you
add a network. The Zero W can't be a hotspot and a client at once, so:
  * adding/choosing a network in the UI -> leave setup mode and connect to it;
  * nobody joined and the UI unused for `setup_minutes` -> go back to client mode and retry the
    saved networks (they may just have been out of range), then fall back again if still nothing.
Recording and archiving never depend on any of this; only uploads need a connection.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Callable, Optional

from . import db
from .config import Config
from .wifi import Nmcli, WifiError

log = logging.getLogger(__name__)

MAX_BACKOFF = 3600
ACTIONS = ("wifi-restart", "wifi-reset-driver")
UI_ACTIVE_SECONDS = 180  # setup page viewed this recently -> someone is setting it up


class Watchdog:
    """Recovers a link that is up but passing no traffic, with escalation and backoff."""

    def __init__(self, stuck_seconds: float, recover_fn: Callable[[str], None],
                 now: Callable[[], float] = time.time):
        self.stuck = stuck_seconds
        self.backoff = stuck_seconds
        self.recover = recover_fn
        self.now = now
        self.last_ok = now()  # grace period after start
        self.last_action = 0.0
        self.recoveries = 0

    def reset(self) -> None:
        self.last_ok = self.now()
        self.backoff = self.stuck
        self.recoveries = 0

    def observe(self, ok: bool, detail: str = "") -> Optional[str]:
        """Feed one probe result; returns the recovery action taken, if any."""
        t = self.now()
        if ok:
            if self.recoveries:
                log.info("wifi back after %d recovery attempt(s)", self.recoveries)
            self.reset()
            return None
        if t - max(self.last_ok, self.last_action) < self.backoff:
            return None
        action = ACTIONS[self.recoveries % len(ACTIONS)]
        log.warning("wifi dead for %.0f min (%s): %s", (t - self.last_ok) / 60, detail, action)
        try:
            self.recover(action)
        except Exception as e:  # keep watching even if recovery itself fails
            log.error("%s failed: %s", action, e)
        self.recoveries += 1
        self.last_action = self.now()
        if self.recoveries >= len(ACTIONS):
            self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        return action


class Manager:
    def __init__(self, cfg: Config, backend, conn, recover_fn: Callable[[str], None],
                 now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.wifi = backend
        self.conn = conn
        self.now = now
        self.watchdog = Watchdog(float(cfg.get("network.stuck_minutes")) * 60, recover_fn, now)
        self.watchdog_enabled = bool(cfg.get("network.watchdog"))
        self.fallback = float(cfg.get("network.fallback_minutes")) * 60
        self.setup_time = float(cfg.get("network.setup_minutes")) * 60
        self.hotspot_enabled = bool(cfg.get("network.setup_hotspot")) and bool(cfg.get("network.setup_password"))
        if cfg.get("network.setup_hotspot") and not cfg.get("network.setup_password"):
            log.warning("setup hotspot disabled: network.setup_password is not set")
        self.mode = "client"
        self.disconnected_since: Optional[float] = now()
        self.setup_since = 0.0
        self.last_activity = 0.0
        self.state, self.detail = "disconnected", ""
        self.action: Optional[str] = None

    # --- transitions -----------------------------------------------------------------------

    def enter_setup(self, reason: str) -> None:
        log.warning("starting setup hotspot %r: %s", self.cfg.get("network.setup_ssid"), reason)
        try:  # remember what's around: scanning is impossible while we are the hotspot
            db.set_status(self.conn, "wifi_scan", {"at": self.now(), "networks": self.wifi.scan()})
        except WifiError as e:
            log.info("scan before setup failed: %s", e)
        try:
            self.wifi.start_hotspot(self.cfg.get("network.setup_ssid"), self.cfg.get("network.setup_password"),
                                    self.cfg.get("network.setup_address"))
        except WifiError as e:
            log.error("could not start setup hotspot: %s", e)
            self.disconnected_since = self.now()  # retry after another fallback period
            return
        self.mode = "setup"
        self.setup_since = self.last_activity = self.now()

    def enter_client(self, reason: str, uuid: Optional[str] = None) -> None:
        log.info("leaving setup mode: %s", reason)
        if self.mode == "setup":
            self.wifi.stop_hotspot()
        self.mode = "client"
        self.disconnected_since = self.now()
        self.watchdog.reset()
        try:
            self.wifi.connect(uuid)
        except WifiError as e:
            log.info("connect: %s", e)  # not fatal: falls back to setup mode again later

    # --- main step -------------------------------------------------------------------------

    def tick(self) -> None:
        self.action = None
        request = db.take_request(self.conn, "wifi_connect")
        if request:
            uuid = request.get("uuid") if isinstance(request, dict) else None
            self.enter_client("network chosen in the UI", uuid)
            return

        if self.mode == "setup":
            self.tick_setup()
        else:
            self.tick_client()

    def tick_client(self) -> None:
        self.state, self.detail = self.wifi.probe()
        t = self.now()
        if self.state == "ok":
            self.disconnected_since = None
            self.watchdog.observe(True)
            return
        if self.state == "disconnected" and self.hotspot_enabled:
            if self.disconnected_since is None:
                self.disconnected_since = t
            try:
                saved = len(self.wifi.known())
            except WifiError:
                saved = 1
            if saved == 0:
                self.enter_setup("no wifi network saved")
            elif t - self.disconnected_since >= self.fallback:
                self.enter_setup(f"no saved network reachable for {self.fallback / 60:.0f} min")
            return
        # 'stuck' (or disconnected with no hotspot to fall back to): watchdog territory
        if self.watchdog_enabled:
            self.action = self.watchdog.observe(False, self.detail)
            if self.action:
                db.set_status(self.conn, "wifi_recovery", {"at": t, "action": self.action, "detail": self.detail})

    def tick_setup(self) -> None:
        t = self.now()
        self.state, self.detail = "setup", f"running setup hotspot {self.cfg.get('network.setup_ssid')}"
        seen = db.status(self.conn, "wifi_ui_seen") or 0
        if self.wifi.stations() > 0 or t - seen < UI_ACTIVE_SECONDS:
            self.last_activity = t
            return
        try:
            saved = len(self.wifi.known())
        except WifiError:
            saved = 1
        if saved and t - self.last_activity >= self.setup_time:
            self.enter_client("nobody used the setup hotspot, retrying saved networks")

    def status(self) -> dict:
        return {
            "mode": self.mode, "ok": self.mode == "client" and self.state == "ok", "state": self.state,
            "detail": self.detail, "last_ok": self.watchdog.last_ok, "recoveries": self.watchdog.recoveries,
            "disconnected_since": self.disconnected_since, "setup_since": self.setup_since if self.mode == "setup" else None,
            "setup_ssid": self.cfg.get("network.setup_ssid"), "setup_address": self.cfg.get("network.setup_address"),
            "hotspot_enabled": self.hotspot_enabled, "at": self.now(),
        }


def run(cfg: Config) -> None:
    if not sys.platform.startswith("linux"):
        log.info("network daemon only runs on Linux")
        return
    if not cfg.get("network.watchdog") and not cfg.get("network.setup_hotspot"):
        log.info("wifi watchdog and setup hotspot both disabled in config")
        return
    conn = db.open_db(cfg)
    helper = cfg.get("car.priv_helper")

    def recover(action: str) -> None:
        subprocess.run(["sudo", "-n", helper, action], check=True, capture_output=True, timeout=120)

    mgr = Manager(cfg, Nmcli(cfg.get("network.interface")), conn, recover)
    log.info("network daemon started on %s", cfg.get("network.interface"))
    interval = float(cfg.get("network.check_interval"))
    while True:
        try:
            mgr.tick()
        except Exception:
            log.exception("network tick failed")
        db.set_status(conn, "wifi", mgr.status())
        # short sleeps so a network chosen in the UI is acted on quickly
        deadline = time.time() + interval
        while time.time() < deadline:
            time.sleep(2)
            if db.kv_get(conn, "request:wifi_connect"):
                break
