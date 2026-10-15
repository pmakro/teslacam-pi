from datetime import datetime

from teslacam import tesla


def test_parse_clip_name():
    clip = tesla.parse_clip_name("2026-10-01_13-53-10-left_repeater.mp4")
    assert clip.camera == "left_repeater"
    assert clip.start == datetime(2026, 10, 1, 13, 53, 10)
    assert tesla.parse_clip_name("event.json") is None
    assert tesla.parse_clip_name("2026-13-01_13-53-10-front.mp4") is None


def test_trigger_window_priority():
    trigger = datetime(2026, 10, 1, 14, 3, 22)
    assert tesla.file_priority("2026-10-01_14-02-30-front.mp4", trigger) == 0
    assert tesla.file_priority("2026-10-01_14-00-30-front.mp4", trigger) == 0  # ends 14:01:30 > 14:01:22
    assert tesla.file_priority("2026-10-01_14-00-10-front.mp4", trigger) == 1  # ends 14:01:10
    assert tesla.file_priority("2026-10-01_14-04-30-front.mp4", trigger) == 1  # starts after T+1
    assert tesla.file_priority("event.json", trigger) == 0


def test_event_json(tmp_path):
    p = tmp_path / "event.json"
    p.write_text('{"timestamp":"2026-10-01T14:03:22","city":"Athens","est_lat":"37.9","reason":"sentry"}')
    meta = tesla.read_event_json(p)
    assert meta.timestamp == datetime(2026, 10, 1, 14, 3, 22)
    assert meta.city == "Athens" and meta.lat == 37.9 and meta.lon is None
    assert tesla.read_event_json(tmp_path / "missing.json").timestamp is None
