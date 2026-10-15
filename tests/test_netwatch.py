import pytest

from teslacam import db
from teslacam.netwatch import MAX_BACKOFF, Manager, Watchdog
from teslacam.wifi import WifiError


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --- watchdog ----------------------------------------------------------------------------------

def make_dog(stuck=600):
    clock, actions = Clock(), []
    return Watchdog(stuck, actions.append, now=clock), clock, actions


def feed(dog, clock, ok, seconds, step=30):
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        dog.observe(ok, "detail")


def test_watchdog_healthy_never_recovers():
    dog, clock, actions = make_dog()
    feed(dog, clock, True, 7200)
    assert actions == []


def test_watchdog_tolerates_short_outage():
    dog, clock, actions = make_dog()
    feed(dog, clock, False, 540)
    feed(dog, clock, True, 60)
    assert actions == []


def test_watchdog_escalates_and_backs_off():
    dog, clock, actions = make_dog()
    feed(dog, clock, False, 600)
    assert actions == ["wifi-restart"]
    feed(dog, clock, False, 600)
    assert actions == ["wifi-restart", "wifi-reset-driver"]
    feed(dog, clock, False, 600)  # backoff doubled to 20 min
    assert len(actions) == 2
    feed(dog, clock, False, 600)
    assert actions[2] == "wifi-restart"
    feed(dog, clock, False, 6 * 3600)
    assert dog.backoff == MAX_BACKOFF
    assert len(actions) <= 3 + 1 + 6  # about hourly once capped


def test_watchdog_recovery_resets_backoff():
    dog, clock, actions = make_dog()
    feed(dog, clock, False, 3 * 3600)
    dog.observe(True)
    assert dog.backoff == 600 and dog.recoveries == 0


def test_watchdog_survives_failing_recovery():
    clock = Clock()

    def boom(action):
        raise RuntimeError("nmcli missing")

    dog = Watchdog(60, boom, now=clock)
    feed(dog, clock, False, 300)
    assert dog.recoveries >= 1


# --- setup hotspot manager ---------------------------------------------------------------------

class FakeWifi:
    def __init__(self):
        self.link = "disconnected"
        self.saved = [{"name": "Car", "uuid": "u1", "ssid": "Car", "priority": "10"}]
        self.hotspot = False
        self.station_count = 0
        self.connects = []
        self.fail_hotspot = False

    def probe(self):
        if self.hotspot:
            return "disconnected", "hotspot"
        return self.link, self.link

    def known(self):
        return list(self.saved)

    def scan(self):
        return [{"ssid": "Car", "signal": 70, "secure": True}]

    def start_hotspot(self, ssid, password, address):
        if self.fail_hotspot:
            raise WifiError("AP mode not supported")
        self.hotspot = True

    def stop_hotspot(self):
        self.hotspot = False

    def connect(self, uuid=None):
        self.connects.append(uuid)

    def stations(self):
        return self.station_count if self.hotspot else 0


@pytest.fixture
def mgr(env):
    env.cfg.data["network"]["setup_password"] = "supersecret"
    clock, wifi, recovered = Clock(), FakeWifi(), []
    m = Manager(env.cfg, wifi, env.conn, recovered.append, now=clock)
    m.clock, m.fake, m.recovered = clock, wifi, recovered
    return m


def run_for(m, seconds, step=30):
    end = m.clock.t + seconds
    while m.clock.t < end:
        m.clock.t += step
        m.tick()


def test_connected_stays_client(mgr):
    mgr.fake.link = "ok"
    run_for(mgr, 3600)
    assert mgr.mode == "client" and not mgr.fake.hotspot and mgr.recovered == []


def test_falls_back_to_setup_after_fallback_minutes(mgr):
    run_for(mgr, 150)
    assert mgr.mode == "client"
    run_for(mgr, 60)
    assert mgr.mode == "setup" and mgr.fake.hotspot
    assert db.status(mgr.conn, "wifi_scan")["networks"][0]["ssid"] == "Car"  # scanned before going AP


def test_no_saved_networks_goes_to_setup_immediately_and_stays(mgr):
    mgr.fake.saved = []
    run_for(mgr, 30)
    assert mgr.mode == "setup"
    run_for(mgr, 3600)
    assert mgr.mode == "setup"  # nothing to retry


def test_unused_setup_hotspot_retries_saved_networks(mgr):
    run_for(mgr, 210)
    assert mgr.mode == "setup"
    run_for(mgr, 300)
    assert mgr.mode == "client" and not mgr.fake.hotspot and mgr.fake.connects == [None]
    mgr.fake.link = "ok"  # the hotspot came back in range
    run_for(mgr, 600)
    assert mgr.mode == "client"


def test_setup_hotspot_kept_while_in_use(mgr):
    run_for(mgr, 210)
    mgr.fake.station_count = 1
    run_for(mgr, 1800)
    assert mgr.mode == "setup"
    mgr.fake.station_count = 0
    db.set_status(mgr.conn, "wifi_ui_seen", mgr.clock.t)
    run_for(mgr, 120)
    assert mgr.mode == "setup"


def test_ui_choice_leaves_setup(mgr):
    run_for(mgr, 210)
    db.request(mgr.conn, "wifi_connect", {"uuid": "u2"})
    mgr.clock.t += 2
    mgr.tick()
    assert mgr.mode == "client" and not mgr.fake.hotspot and mgr.fake.connects == ["u2"]


def test_stuck_link_uses_watchdog_not_hotspot(mgr):
    mgr.fake.link = "stuck"
    run_for(mgr, 700)
    assert mgr.mode == "client" and mgr.recovered == ["wifi-restart"]
    assert db.status(mgr.conn, "wifi_recovery")["action"] == "wifi-restart"


def test_hotspot_disabled_without_password(env):
    env.cfg.data["network"]["setup_password"] = ""
    clock, wifi, recovered = Clock(), FakeWifi(), []
    m = Manager(env.cfg, wifi, env.conn, recovered.append, now=clock)
    m.clock = clock
    run_for(m, 1200)
    assert m.mode == "client" and not wifi.hotspot
    assert recovered  # watchdog handles the dead link instead


def test_hotspot_start_failure_retries_later(mgr):
    mgr.fake.fail_hotspot = True
    run_for(mgr, 210)
    assert mgr.mode == "client"
    mgr.fake.fail_hotspot = False
    run_for(mgr, 200)
    assert mgr.mode == "setup"
