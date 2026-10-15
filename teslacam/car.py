"""Access to the drive the car records onto.

ImageCar (Pi): the car writes into a disk image exposed through the USB mass-storage gadget.
We never mount the live image while the car owns it. Instead a reflink copy (instant on XFS,
no extra space until the original changes) is loop-mounted read-only for archiving. To delete
from the car drive, the gadget is briefly detached, the image is mounted read-write, cleaned,
fsck'd and handed back.

DirectoryCar (dev/testing): a plain directory containing TeslaCam/.

All privileged steps go through a root-owned helper script with a fixed set of actions
(see scripts/priv.sh), so the Python services run as an unprivileged user.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Tuple

from .config import Config

log = logging.getLogger(__name__)


class CarDriveError(RuntimeError):
    pass


class CarDrive:
    def snapshot(self):  # -> contextmanager yielding Optional[Path] (TeslaCam root)
        raise NotImplementedError

    def read_write(self):  # -> contextmanager yielding Path (TeslaCam root)
        raise NotImplementedError

    def idle_seconds(self) -> float:
        raise NotImplementedError

    def eject(self) -> None:
        raise NotImplementedError

    def attach(self) -> None:
        raise NotImplementedError

    def is_attached(self) -> bool:
        return True

    @staticmethod
    def usage(root: Path) -> Tuple[int, int]:
        u = shutil.disk_usage(root)
        return u.total, u.free


class DirectoryCar(CarDrive):
    def __init__(self, source_dir: str):
        self.root = Path(source_dir) / "TeslaCam"
        self.attached = True

    @contextmanager
    def snapshot(self) -> Iterator[Optional[Path]]:
        yield self.root if self.root.is_dir() else None

    @contextmanager
    def read_write(self) -> Iterator[Path]:
        yield self.root

    def idle_seconds(self) -> float:
        newest = 0.0
        if self.root.is_dir():
            for dirpath, _, filenames in os.walk(self.root):
                for name in filenames:
                    try:
                        newest = max(newest, os.stat(os.path.join(dirpath, name)).st_mtime)
                    except OSError:
                        pass
        return time.time() - newest if newest else float("inf")

    def eject(self) -> None:
        self.attached = False

    def attach(self) -> None:
        self.attached = True

    def is_attached(self) -> bool:
        return self.attached


class ImageCar(CarDrive):
    def __init__(self, cfg: Config):
        self.image = Path(cfg.get("car.image_path"))
        self.snap_mount = Path(cfg.get("car.snapshot_mount"))
        self.rw_mount = Path(cfg.get("car.rw_mount"))
        self.helper = cfg.get("car.priv_helper")

    def _priv(self, action: str, timeout: int = 600) -> str:
        cmd = ["sudo", "-n", self.helper, action]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise CarDriveError(f"{action}: timed out") from e
        if res.returncode != 0:
            raise CarDriveError(f"{action}: {res.stderr.strip() or res.stdout.strip()}")
        return res.stdout.strip()

    @contextmanager
    def snapshot(self) -> Iterator[Optional[Path]]:
        if not self.image.exists():
            yield None
            return
        self._priv("snapshot-mount")
        try:
            root = self.snap_mount / "TeslaCam"
            yield root if root.is_dir() else None
        finally:
            try:
                self._priv("snapshot-umount")
            except CarDriveError:
                log.exception("failed to release snapshot")

    @contextmanager
    def read_write(self) -> Iterator[Path]:
        self._priv("gadget-off")
        try:
            self._priv("rw-mount")
            try:
                yield self.rw_mount / "TeslaCam"
            finally:
                self._priv("rw-umount")  # includes fsck
        finally:
            self._priv("gadget-on")

    def idle_seconds(self) -> float:
        try:
            return time.time() - self.image.stat().st_mtime
        except OSError:
            return 0.0

    def eject(self) -> None:
        self._priv("gadget-off")

    def attach(self) -> None:
        self._priv("gadget-on")

    def is_attached(self) -> bool:
        try:
            return self._priv("gadget-status", timeout=10) == "on"
        except CarDriveError:
            return False


def from_config(cfg: Config) -> CarDrive:
    if cfg.get("car.mode") == "directory":
        return DirectoryCar(cfg.get("car.source_dir"))
    return ImageCar(cfg)
