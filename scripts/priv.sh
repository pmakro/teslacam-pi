#!/bin/bash
# Privileged operations for the TeslaCam services, run via sudo by the unprivileged `teslacam` user.
# Only the fixed actions below are accepted; all paths come from the root-owned env file.
#
#   gadget-on | gadget-off | gadget-status   present / remove the car drive over USB
#   snapshot-mount | snapshot-umount         reflink snapshot of the car drive, mounted read-only
#   rw-mount | rw-umount                     mount the car drive read-write (gadget must be off), fsck on release
#   wifi-restart | wifi-reset-driver         recover a wedged wifi link (radio toggle / driver reload)
#   reboot
set -euo pipefail

ENV_FILE=/etc/teslacam/priv.env
# shellcheck source=/dev/null
source "$ENV_FILE"
: "${IMAGE:?}" "${SNAPSHOT:?}" "${SNAP_MOUNT:?}" "${RW_MOUNT:?}" "${OFFSET:?}" "${OWNER_UID:?}" "${OWNER_GID:?}"

RW_LOOP_FILE=/run/teslacam-rw.loop
MOUNT_OPTS="uid=${OWNER_UID},gid=${OWNER_GID},fmask=0133,dmask=0022"

gadget_loaded() { [ -d /sys/module/g_mass_storage ]; }

fsck_image() {
  local dev=$1
  # exfatprogs: -y repairs without asking; a non-zero exit after repairs is not fatal for us.
  fsck.exfat -y "$dev" >/dev/null 2>&1 || true
}

case "${1:-}" in
  gadget-on)
    if ! gadget_loaded; then
      [ -f "$IMAGE" ] || { echo "car drive image $IMAGE missing" >&2; exit 1; }
      modprobe g_mass_storage file="$IMAGE" removable=1 ro=0 stall=0 \
        iManufacturer=TeslaCam iProduct=TeslaCam iSerialNumber=TESLACAM0001
    fi
    ;;
  gadget-off)
    if gadget_loaded; then
      sync
      modprobe -r g_mass_storage
    fi
    ;;
  gadget-status)
    if gadget_loaded; then echo on; else echo off; fi
    ;;
  snapshot-mount)
    mountpoint -q "$SNAP_MOUNT" && umount "$SNAP_MOUNT"
    rm -f "$SNAPSHOT"
    # Instant, space-sharing copy on XFS. The car keeps writing to the original meanwhile.
    cp --reflink=always "$IMAGE" "$SNAPSHOT"
    mkdir -p "$SNAP_MOUNT"
    # exFAT tolerates a dirty (in-use) volume when mounted read-only.
    mount -t exfat -o "loop,ro,offset=${OFFSET},${MOUNT_OPTS}" "$SNAPSHOT" "$SNAP_MOUNT"
    ;;
  snapshot-umount)
    mountpoint -q "$SNAP_MOUNT" && umount "$SNAP_MOUNT"
    rm -f "$SNAPSHOT"
    ;;
  rw-mount)
    if gadget_loaded; then echo "refusing: car drive is still attached to the car" >&2; exit 1; fi
    mountpoint -q "$RW_MOUNT" && { echo "already mounted" >&2; exit 1; }
    loop=$(losetup --find --show --offset "$OFFSET" "$IMAGE")
    echo "$loop" > "$RW_LOOP_FILE"
    fsck_image "$loop"
    mkdir -p "$RW_MOUNT"
    mount -t exfat -o "$MOUNT_OPTS" "$loop" "$RW_MOUNT"
    ;;
  rw-umount)
    if mountpoint -q "$RW_MOUNT"; then
      sync
      umount "$RW_MOUNT"
    fi
    if [ -f "$RW_LOOP_FILE" ]; then
      loop=$(cat "$RW_LOOP_FILE")
      fsck_image "$loop"
      losetup -d "$loop" || true
      rm -f "$RW_LOOP_FILE"
    fi
    ;;
  wifi-restart)
    nmcli radio wifi off
    sleep 3
    nmcli radio wifi on
    ;;
  wifi-reset-driver)
    # The usual cure for a wedged brcmfmac chip on the Pi Zero W.
    modprobe -r brcmfmac || true
    sleep 3
    modprobe brcmfmac
    ;;
  reboot)
    systemctl reboot
    ;;
  *)
    echo "usage: $0 {gadget-on|gadget-off|gadget-status|snapshot-mount|snapshot-umount|rw-mount|rw-umount|wifi-restart|wifi-reset-driver|reboot}" >&2
    exit 2
    ;;
esac
