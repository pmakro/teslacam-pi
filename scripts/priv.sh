#!/bin/bash
# Privileged operations for the TeslaCam services, run via sudo by the unprivileged `teslacam` user.
# Only the fixed actions below are accepted; all paths come from the root-owned env file.
#
#   gadget-on | gadget-off | gadget-status   present / remove the car drive over USB
#   snapshot-mount | snapshot-umount         reflink snapshot of the car drive, mounted read-only
#   rw-mount | rw-umount                     mount the car drive read-write (gadget must be off), fsck on release
#   wifi-restart | wifi-reset-driver         recover a wedged wifi link (radio toggle / driver reload)
#   hotspot-start | hotspot-stop             run the setup hotspot (settings come from config.toml)
#   reboot
set -euo pipefail

ENV_FILE=/etc/teslacam/priv.env
# shellcheck source=/dev/null
source "$ENV_FILE"
: "${IMAGE:?}" "${SNAPSHOT:?}" "${SNAP_MOUNT:?}" "${RW_MOUNT:?}" "${OFFSET:?}" "${OWNER_UID:?}" "${OWNER_GID:?}"

RW_LOOP_FILE=/run/teslacam-rw.loop
CONFIG_FILE=/etc/teslacam/config.toml
HOTSPOT_STATE=/run/teslacam-hotspot
DHCP_LEASES=/run/teslacam-hotspot.leases
MOUNT_OPTS="uid=${OWNER_UID},gid=${OWNER_GID},fmask=0133,dmask=0022"

gadget_loaded() { [ -d /sys/module/g_mass_storage ]; }

fsck_image() {
  local dev=$1
  # exfatprogs: -y repairs without asking; a non-zero exit after repairs is not fatal for us.
  fsck.exfat -y "$dev" >/dev/null 2>&1 || true
}

# The usual cure for a wedged brcmfmac chip on the Pi Zero W. Newer kernels load a vendor module
# (brcmfmac_cyw on the Zero W) that holds brcmfmac, so whatever holds it has to go first.
reload_wifi_driver() {
  local holder output
  for holder in /sys/module/brcmfmac/holders/*; do
    [ -e "$holder" ] && modprobe -r "$(basename "$holder")" 2>/dev/null || true
  done
  if ! output=$(modprobe -r brcmfmac 2>&1); then
    logger -t teslacam-wifi "could not unload brcmfmac: $output"
  fi
  sleep 3
  modprobe brcmfmac
}

# Safe to call when no hotspot is running: without the state directory it changes nothing, so
# it can't take down a working wifi connection.
hotspot_stop() {
  [ -d "$HOTSPOT_STATE" ] || return 0
  local iface pidfile
  iface=$(cat "$HOTSPOT_STATE/iface" 2>/dev/null || echo wlan0)
  for pidfile in "$HOTSPOT_STATE"/*.pid; do
    [ -f "$pidfile" ] && kill "$(cat "$pidfile")" 2>/dev/null || true
  done
  sleep 1
  ip addr flush dev "$iface" 2>/dev/null || true
  rm -rf "$HOTSPOT_STATE" "$DHCP_LEASES"
  # After hostapd the Pi Zero W rejoined home wifi fine, then a few minutes later its transmit
  # rate collapsed and packets were lost. Start the client connection with fresh firmware.
  reload_wifi_driver || true
  local waited=0
  while [ ! -e "/sys/class/net/$iface" ] && [ "$waited" -lt 15 ]; do
    sleep 1
    waited=$((waited + 1))
  done
  nmcli device set "$iface" managed yes || true
  sleep 6  # the reloaded chip needs its firmware, and NetworkManager a moment to pick it up
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
    reload_wifi_driver
    ;;
  hotspot-start)
    hotspot_stop
    install -d -m 700 "$HOTSPOT_STATE"
    trap hotspot_stop ERR
    settings=$(python3 -I "$(dirname "$(readlink -f "$0")")/hotspot_conf.py" "$CONFIG_FILE" "$HOTSPOT_STATE/hostapd.conf")
    read -r iface address dhcp_start dhcp_end <<< "$settings"
    echo "$iface" > "$HOTSPOT_STATE/iface"
    nmcli device set "$iface" managed no
    sleep 2
    hostapd -B -P "$HOTSPOT_STATE/hostapd.pid" "$HOTSPOT_STATE/hostapd.conf" >/dev/null
    ip addr flush dev "$iface"
    ip addr add "$address/24" dev "$iface"
    # Every DNS name answers with the Pi's address, so phones pop up the Wifi page by themselves.
    dnsmasq --conf-file=/dev/null --interface="$iface" --bind-interfaces --no-resolv --no-hosts \
      --address="/#/$address" --dhcp-range="$dhcp_start,$dhcp_end,255.255.255.0,2h" \
      --dhcp-leasefile="$DHCP_LEASES" --pid-file="$HOTSPOT_STATE/dnsmasq.pid"
    trap - ERR
    ;;
  hotspot-stop)
    hotspot_stop
    ;;
  reboot)
    systemctl reboot
    ;;
  *)
    echo "usage: $0 {gadget-on|gadget-off|gadget-status|snapshot-mount|snapshot-umount|rw-mount|rw-umount|wifi-restart|wifi-reset-driver|hotspot-start|hotspot-stop|reboot}" >&2
    exit 2
    ;;
esac
