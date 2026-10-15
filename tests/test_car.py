import subprocess

import pytest

from teslacam import car as car_mod
from teslacam.car import CarDriveError, ImageCar


class FakeHelper:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, cmd, **kwargs):
        assert cmd[:2] == ["sudo", "-n"]
        action = cmd[3]
        self.calls.append(action)
        rc = 1 if action in self.fail else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="on" if action == "gadget-status" else "", stderr="boom")


@pytest.fixture
def image_car(env, monkeypatch, tmp_path):
    image = tmp_path / "cam_disk.bin"
    image.write_bytes(b"")
    env.cfg.data["car"].update(mode="image", image_path=str(image),
                               snapshot_mount=str(tmp_path / "snap"), rw_mount=str(tmp_path / "rw"))
    helper = FakeHelper()
    monkeypatch.setattr(car_mod.subprocess, "run", helper)
    return ImageCar(env.cfg), helper


def test_snapshot_always_released(image_car):
    car, helper = image_car
    with pytest.raises(RuntimeError):
        with car.snapshot():
            raise RuntimeError("scan crashed")
    assert helper.calls == ["snapshot-mount", "snapshot-umount"]


def test_read_write_reattaches_gadget_on_error(image_car):
    car, helper = image_car
    with pytest.raises(RuntimeError):
        with car.read_write():
            raise RuntimeError("cleanup crashed")
    assert helper.calls == ["gadget-off", "rw-mount", "rw-umount", "gadget-on"]


def test_read_write_reattaches_when_mount_fails(image_car):
    car, helper = image_car
    helper.fail.add("rw-mount")
    with pytest.raises(CarDriveError):
        with car.read_write():
            pass
    assert helper.calls == ["gadget-off", "rw-mount", "gadget-on"]


def test_status(image_car):
    car, _ = image_car
    assert car.is_attached() is True
