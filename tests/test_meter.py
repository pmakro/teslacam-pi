from datetime import date, datetime

from teslacam import db, meter


def test_period_start():
    assert meter.period_start(datetime(2026, 10, 15), 1) == date(2026, 10, 1)
    assert meter.period_start(datetime(2026, 10, 4), 5) == date(2026, 9, 5)
    assert meter.period_start(datetime(2026, 1, 4), 5) == date(2025, 12, 5)


def _counters(tmp_path, iface, rx, tx):
    d = tmp_path / "net" / iface / "statistics"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rx_bytes").write_text(str(rx))
    (d / "tx_bytes").write_text(str(tx))
    return tmp_path / "net"


def test_sample_handles_reboots(env, tmp_path):
    env.cfg.data["budget"]["interface"] = "ts0"
    sysfs = _counters(tmp_path, "ts0", 100, 900)
    assert meter.sample(env.conn, env.cfg, sysfs, boot_id="a") == 0  # first sample is a baseline
    _counters(tmp_path, "ts0", 200, 1800)
    assert meter.sample(env.conn, env.cfg, sysfs, boot_id="a") == 1000
    _counters(tmp_path, "ts0", 50, 50)  # reboot: counters restart
    assert meter.sample(env.conn, env.cfg, sysfs, boot_id="b") == 100
    assert meter.used_bytes(env.conn, env.cfg) == 1100


def test_usage_rolls_over(env):
    meter.add_usage(env.conn, env.cfg, 500, now=datetime(2026, 9, 30))
    meter.add_usage(env.conn, env.cfg, 700, now=datetime(2026, 10, 1))
    assert meter.used_bytes(env.conn, env.cfg, now=datetime(2026, 10, 2)) == 700
    db.set_setting(env.conn, "budget.billing_day", 2)
    # with billing day 2, Oct 1 belongs to the September period
    assert meter.used_bytes(env.conn, env.cfg, now=datetime(2026, 10, 1)) == 0  # stored under old key
