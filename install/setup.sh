#!/bin/bash
# TeslaCam installer for a Raspberry Pi Zero W / Zero 2 W running Raspberry Pi OS Lite (Bookworm).
# Run from a checkout of this repo on the Pi:   sudo ./install/setup.sh
# Safe to run again (e.g. after `git pull`): existing data, images and config are kept.
#
# Tunables (environment):
#   CAM_SIZE_GB   size of the drive the car sees           (default 32)
#   HOSTNAME_NEW  hostname, UI at http://<name>.local      (default teslacam)
#   SKIP_TAILSCALE=1  don't install Tailscale
set -euo pipefail

CAM_SIZE_GB=${CAM_SIZE_GB:-32}
HOSTNAME_NEW=${HOSTNAME_NEW:-teslacam}
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/teslacam
ETC=/etc/teslacam
USER_NAME=teslacam
BACKING_FILE=/var/lib/teslacam/backingfiles.xfs
BACKING=/backingfiles
IMAGE=$BACKING/cam_disk.bin
ARCHIVE=/archive
OFFSET=1048576   # the exFAT partition inside cam_disk.bin starts at 1 MiB

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
grep -qi raspberry /proc/device-tree/model 2>/dev/null || warn "this does not look like a Raspberry Pi, continuing anyway"

BOOT=/boot/firmware
[ -f $BOOT/config.txt ] || BOOT=/boot
REBOOT_NEEDED=0

# ---------------------------------------------------------------------------------------------
log "Installing packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-flask python3-waitress rsync openssh-client \
  exfatprogs xfsprogs util-linux avahi-daemon curl ca-certificates \
  network-manager dnsmasq-base hostapd iw polkitd >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 9)' || die "Python 3.9+ required"

# ---------------------------------------------------------------------------------------------
log "Enabling USB gadget mode (dwc2)"
if ! grep -q '^dtoverlay=dwc2' $BOOT/config.txt; then
  printf '\n[all]\ndtoverlay=dwc2\n' >> $BOOT/config.txt
  REBOOT_NEEDED=1
fi
grep -qx dwc2 /etc/modules || { echo dwc2 >> /etc/modules; REBOOT_NEEDED=1; }
# No systemd hardware watchdog: the Pi's chip allows at most 15 s, and on a single-core Zero W a
# `systemctl daemon-reload` (run by this script, apt and package installs) can block systemd for
# longer, so the watchdog hard-reset the Pi mid-install. A reset also pulls the drive from the car.
# Remove the setting left by earlier versions of this script and switch it off right away, before
# the daemon-reloads below (systemd 249+ allows changing it at runtime).
rm -f /etc/systemd/system.conf.d/teslacam-watchdog.conf
busctl set-property org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager \
  RuntimeWatchdogUSec t 0 2>/dev/null || true

# ---------------------------------------------------------------------------------------------
log "Creating user $USER_NAME"
id $USER_NAME >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin $USER_NAME
UID_N=$(id -u $USER_NAME); GID_N=$(id -g $USER_NAME)

# ---------------------------------------------------------------------------------------------
log "Preparing storage"
mkdir -p /var/lib/teslacam $BACKING $ARCHIVE /mnt/teslacam/snap /mnt/teslacam/rw
if [ ! -f $BACKING_FILE ]; then
  need_gb=$((CAM_SIZE_GB + 4))
  avail_gb=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
  [ "$avail_gb" -gt $((need_gb + 8)) ] || die "not enough space: need ${need_gb}G for the car drive + room for the archive, have ${avail_gb}G"
  echo "allocating ${need_gb}G for the car drive"
  # Fully allocated so the car can always write, even if the archive fills the SD card.
  fallocate -l "${need_gb}G" $BACKING_FILE
  mkfs.xfs -q -m reflink=1 -L backingfiles $BACKING_FILE
fi
if ! grep -q " $BACKING " /etc/fstab; then
  echo "$BACKING_FILE $BACKING xfs loop,noatime 0 0" >> /etc/fstab
fi
mountpoint -q $BACKING || mount $BACKING

if [ ! -f $IMAGE ]; then
  echo "creating ${CAM_SIZE_GB}G exFAT car drive"
  truncate -s "${CAM_SIZE_GB}G" $IMAGE
  echo 'label: dos
start=2048, type=7' | sfdisk -q $IMAGE
  loop=$(losetup --find --show --offset $OFFSET $IMAGE)
  mkfs.exfat -L TESLACAM "$loop" >/dev/null
  mkdir -p /mnt/teslacam/rw
  mount -t exfat "$loop" /mnt/teslacam/rw
  mkdir -p /mnt/teslacam/rw/TeslaCam
  umount /mnt/teslacam/rw
  losetup -d "$loop"
fi
chown $USER_NAME:$USER_NAME $ARCHIVE /mnt/teslacam/snap /mnt/teslacam/rw
chmod 755 $BACKING

# ---------------------------------------------------------------------------------------------
log "Installing application to $APP"
mkdir -p $APP
rsync -a --delete --exclude .git --exclude .venv --exclude dev/data --exclude '__pycache__' "$SRC"/ $APP/
chown -R root:root $APP
chmod 755 $APP/scripts/priv.sh

mkdir -p $ETC
cat > $ETC/priv.env <<EOF
IMAGE=$IMAGE
SNAPSHOT=$BACKING/snapshot.bin
SNAP_MOUNT=/mnt/teslacam/snap
RW_MOUNT=/mnt/teslacam/rw
OFFSET=$OFFSET
OWNER_UID=$UID_N
OWNER_GID=$GID_N
EOF
chmod 644 $ETC/priv.env

if [ ! -f $ETC/config.toml ]; then
  cp $APP/install/config.example.toml $ETC/config.toml
  secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s/^secret_key = \"\"/secret_key = \"$secret\"/" $ETC/config.toml
  setup_pw=$(python3 -c 'import secrets, string; a = string.ascii_lowercase + string.digits; print("".join(secrets.choice(a) for _ in range(12)))')
  sed -i "s/^setup_password = \"\"/setup_password = \"$setup_pw\"/" $ETC/config.toml
  CONFIG_CREATED=1
fi
SETUP_SSID=$(sed -n 's/^setup_ssid = "\(.*\)".*/\1/p' $ETC/config.toml)
SETUP_PW=$(sed -n 's/^setup_password = "\(.*\)".*/\1/p' $ETC/config.toml)
chown root:$USER_NAME $ETC/config.toml
chmod 640 $ETC/config.toml

cat > /etc/sudoers.d/teslacam <<EOF
$USER_NAME ALL=(root) NOPASSWD: $APP/scripts/priv.sh
EOF
chmod 440 /etc/sudoers.d/teslacam
visudo -cf /etc/sudoers.d/teslacam >/dev/null

# ---------------------------------------------------------------------------------------------
log "Wifi management"
# Let the services manage NetworkManager (saved networks) without sudo.
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-teslacam-networkmanager.rules <<'EOF'
polkit.addRule(function (action, subject) {
  if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 && subject.user === "teslacam") {
    return polkit.Result.YES;
  }
});
EOF
systemctl enable --now NetworkManager >/dev/null 2>&1 || warn "NetworkManager is not running; wifi management needs it"
# Older installs ran the setup hotspot through NetworkManager; it now uses hostapd (see priv.sh).
rm -f /etc/NetworkManager/dnsmasq-shared.d/teslacam-captive.conf
nmcli connection delete teslacam-setup >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
log "SSH key for uploads"
HOME_DIR=$(getent passwd $USER_NAME | cut -d: -f6)
if [ ! -f "$HOME_DIR/.ssh/id_ed25519" ]; then
  sudo -u $USER_NAME mkdir -p "$HOME_DIR/.ssh"
  sudo -u $USER_NAME ssh-keygen -q -t ed25519 -N '' -C "teslacam@$HOSTNAME_NEW" -f "$HOME_DIR/.ssh/id_ed25519"
fi

# ---------------------------------------------------------------------------------------------
if [ "${SKIP_TAILSCALE:-0}" != 1 ] && ! command -v tailscale >/dev/null; then
  log "Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh || warn "Tailscale install failed; install it manually"
fi

# ---------------------------------------------------------------------------------------------
log "System tuning"
# Keep logs in RAM (less SD wear, nothing to corrupt on power loss).
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' > /etc/systemd/journald.conf.d/teslacam.conf
# Wifi power saving makes the hotspot link flaky.
if [ -d /etc/NetworkManager/conf.d ]; then
  printf '[connection]\nwifi.powersave = 2\n' > /etc/NetworkManager/conf.d/teslacam-wifi.conf
fi
if [ "$(hostname)" != "$HOSTNAME_NEW" ]; then
  hostnamectl set-hostname "$HOSTNAME_NEW"
  sed -i "s/127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
fi
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------------------------
log "Installing services"
cp $APP/install/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
sudo -u $USER_NAME env PYTHONPATH=$APP python3 -m teslacam init-db >/dev/null
systemctl enable teslacam-gadget teslacam-archiver teslacam-uploader teslacam-web teslacam-netwatch >/dev/null
if [ $REBOOT_NEEDED = 0 ]; then
  systemctl restart teslacam-gadget teslacam-archiver teslacam-uploader teslacam-web teslacam-netwatch
fi

# ---------------------------------------------------------------------------------------------
cat <<EOF

$(printf '\033[1m')TeslaCam installed.$(printf '\033[0m')

Next steps:
 1. Tailscale: sudo tailscale up --hostname=$HOSTNAME_NEW
    (install Tailscale on the NAS in the same tailnet, and disable key expiry for both)
 2. Set up the NAS. Synology step by step: $APP/docs/nas-setup-synology.md
    The NAS user "teslacam" needs this key in its ~/.ssh/authorized_keys:

    $(cat "$HOME_DIR/.ssh/id_ed25519.pub")

 3. Edit $ETC/config.toml: [upload] host (NAS Tailscale IP) / user / remote_dir,
    [budget] monthly_gb / billing_day.
    Set a UI password:  sudo -u $USER_NAME env PYTHONPATH=$APP python3 -m teslacam hash-password
    and paste the hash into web.password_hash.
 4. Test the NAS connection (should list the folder without asking for a password):
    sudo -u $USER_NAME rsync --list-only -e "ssh -i $HOME_DIR/.ssh/id_ed25519" <user>@<nas>:<remote_dir>/
 5. sudo systemctl restart teslacam-archiver teslacam-uploader teslacam-web
EOF
[ "${CONFIG_CREATED:-0}" = 1 ] && echo "    (a fresh config was created at $ETC/config.toml)"
if [ $REBOOT_NEEDED = 1 ]; then
  echo
  warn "Reboot required to enable USB gadget mode: sudo reboot"
fi
echo
echo "UI: http://$HOSTNAME_NEW.local (from a device on the same hotspot)"
echo
if [ -n "$SETUP_PW" ]; then
  echo "If the Pi can't reach any saved wifi for 3 minutes it starts its own network:"
  echo "    name: $SETUP_SSID    password: $SETUP_PW    then open http://10.42.0.1/wifi"
  echo "    (keep these: they are also in $ETC/config.toml)"
else
  warn "Setup hotspot disabled: set network.setup_password in $ETC/config.toml to enable it"
fi
