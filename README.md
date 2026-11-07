# TeslaCam Pi

A Raspberry Pi Zero W that plugs into your Tesla as its USB drive. It keeps every **Sentry** and **Saved** clip and uploads them to your **home NAS** over the in-car LTE hotspot, using Tailscale and staying within a **monthly data budget**. It also serves a **web UI** for browsing, playing, downloading, protecting and deleting clips.

> **Unofficial.** This is a hobby project, not affiliated with or endorsed by Tesla, Inc. "Tesla" and "TeslaCam" are Tesla's trademarks and are used here only to describe what the project works with. Use it at your own risk.

This project was inspired by [milesburton/teslacam](https://github.com/milesburton/teslacam), and its snapshot technique follows [marcone/teslausb](https://github.com/marcone/teslausb). There is no Docker and no Node: it is plain Python and systemd, light enough for the single-core ARMv6 Zero W.

```
 Tesla ──USB──▶ Pi Zero W ─────wifi────▶ LTE hotspot ──internet──▶ Tailscale ──▶ home NAS
               │ cam_disk.bin (exFAT, what the car sees)                          (rsync/SSH)
               │   └─ reflink snapshot every 2 min ─▶ /archive (local copies)
               └─ web UI  http://teslacam.local  (phone on the same hotspot)
```

## How it works

| Service | Runs as | What it does |
|---|---|---|
| `teslacam-gadget` | root | Loads `g_mass_storage` with `/backingfiles/cam_disk.bin`, so the car sees a 32 GB USB drive. |
| `teslacam-archiver` | teslacam | Every 2 minutes it takes an instant **reflink snapshot** of the car drive (XFS), mounts the snapshot read-only and copies new `SentryClips`/`SavedClips` files into `/archive`. When the car drive falls below 25% free and the car is idle, it detaches the drive for a few seconds, deletes events that are already archived, fscks the drive and re-attaches it. It also applies local retention. |
| `teslacam-uploader` | teslacam | Sends archived files to the NAS with `rsync` over SSH through Tailscale. Transfers are resumable, it stops at the data budget, and the order is set by priority (below). |
| `teslacam-web` | teslacam | Runs the Flask UI on port 80. |
| `teslacam-netwatch` | teslacam | Network daemon: wifi watchdog, plus the fallback setup hotspot when no saved network is reachable (details below). |

Privileged steps (gadget, mounts, fsck, reboot) go through [`scripts/priv.sh`](scripts/priv.sh). It is a root-owned helper with a fixed list of actions, and the services call it through a single sudoers rule.

**Upload order.** The goal is to spend capped mobile data on the useful part first:
1. events you pressed **Upload now** on
2. the **trigger window** of every event: `event.json`, `thumb.png`, and the clips from 2 minutes before to 1 minute after the trigger, from every camera
3. Saved before Sentry
4. newest events first

The rest of each event follows while the budget allows.

**Data budget.** Traffic is measured on `tailscale0` (so watching clips in the UI over the hotspot isn't counted) and multiplied by 1.06 to cover tunnel overhead. The total is stored for each billing period. Uploads pause at the limit and resume automatically when the next period starts.

**Bad connectivity.**
- The Pi saves clips locally whether or not it is online.
- Resuming: an interrupted file resumes from where it stopped, so bytes already sent aren't paid for twice. Files appear on the NAS only once they are complete.
- Dead links: SSH keepalives and rsync's I/O timeout detect a frozen link within about 60–120 s.
- NAS unreachable: before each batch the Pi runs a quick SSH check. If that fails, it waits and re-checks every minute. With nothing to upload it still checks every 15 minutes (`upload.idle_check_minutes`), so the UI shows a broken link before the first clip needs it.
- Retries: failed batches back off 1 → 2 → 4 → 8 → 15 min.
- What counts as a failure: connection errors (rsync exit codes 10, 12, 30, 35, 255) only mean "offline" and are retried indefinitely. Only real errors, such as permission denied or the NAS disk being full, count toward the 5 attempts after which a file is marked failed.
- Wifi watchdog: if the hotspot's gateway hasn't answered for 10 minutes, the watchdog alternates between toggling the radio and reloading the wifi driver (the usual fix for the Zero W's wedged brcmfmac chip). Between attempts it backs off up to 1 h, so a hotspot that is simply off doesn't cause a restart storm. It never reboots the Pi, since that would pull the drive from the car.

**Local retention.** An event is only deleted from the Pi automatically when it is **fully uploaded and not protected**, and either it is older than *N* days or free space is low (oldest first). Events that haven't been uploaded are never deleted automatically.

## Hardware

- Raspberry Pi Zero W, or Zero 2 W (faster)
- A high-endurance microSD card of **128 GB or more** (for example, SanDisk High Endurance)
- A short **data** micro-USB cable from the Pi's **USB** port (not PWR) to a data-capable USB port in the car (glovebox or centre console, depending on the model)
- An in-car LTE router or hotspot

## Install

1. Flash **Raspberry Pi OS Lite (32-bit, Bookworm)** with Raspberry Pi Imager. When it asks whether to apply OS customisation, choose **Edit settings** and set:
   - **Hostname:** `teslacam`. The Pi is then reachable as `teslacam.local` straight away.
   - **Username and password:** your own login, e.g. `panos`. This is required: Bookworm has no default user, so headless SSH won't work without one. **Don't use `teslacam`**, because the installer creates that account for the services.
   - **Wireless LAN:** the network you'll set up on, and the **wireless LAN country**. The wifi stays off until a country is set. **Home wifi is easiest**; add the car hotspot later from the UI's **Wifi** page. The Pi Zero W only supports **2.4 GHz** networks with **WPA2** (or WPA2/WPA3 mixed), so 5 GHz-only and WPA3-only networks are invisible to it. On an iPhone hotspot, turn on *Maximize Compatibility*.
   - **Locale:** your time zone. The data budget's billing day and the logs use the Pi's clock.
   - **Services → Enable SSH:** preferably *Allow public-key authentication only*, pasting your Mac's **public** key: the single line in `~/.ssh/id_ed25519.pub`, which starts with `ssh-ed25519 AAAA…` (copy it with `pbcopy < ~/.ssh/id_ed25519.pub`; create one with `ssh-keygen -t ed25519` if needed). Never paste the private key (`-----BEGIN OPENSSH PRIVATE KEY-----`).

   The Wireless LAN setting is required for a headless install: a freshly flashed Pi does **not** start the `TeslaCam-Setup` hotspot. That only exists once `setup.sh` has run.

   **Power it properly while installing.** Use a 5V supply of 2A or more on the **PWR** port. A USB outlet on another device (a camera, a router) often can't supply the Zero W's peak current under full load, and the Pi then resets without any log entry.
2. **Get the project onto the Pi.** Boot the Pi and give it 3–5 minutes on first boot: it resizes the SD card, applies the Imager settings and reboots once, and SSH only answers after that (`Connection refused` until then). SSH in with the username you set: `ssh <your-user>@teslacam.local`, or use its IP address from your router. Then use **one** of these:

   **Option A: clone from GitHub (easiest to update).**
   ```bash
   sudo apt install -y git
   git clone https://github.com/pmakro/teslacam-pi.git ~/teslacam
   ```
   To update later, on the Pi: `cd ~/teslacam && git pull && sudo ./install/setup.sh`.

   **Option B: copy from your Mac (no GitHub needed).** From the project folder on the Mac, with the Pi on the same network:
   ```bash
   rsync -av --delete --exclude .git --exclude .venv --exclude dev/data --exclude dev/config.toml ./ <your-user>@teslacam.local:teslacam/
   ```
   If it fails with `rsync: command not found`, run `sudo apt install -y rsync` on the Pi first. To update later, run the same command again, then re-run the installer.

   **Then install (both options).** Run the installer inside `tmux`, so a dropped SSH connection can't stop it halfway (wifi is handed to NetworkManager and Tailscale is started during the install, and either can briefly cut the connection):
   ```bash
   sudo apt install -y tmux
   tmux
   cd ~/teslacam && sudo ./install/setup.sh
   ```
   If the connection drops, SSH in again and run `tmux attach` to get back to the running installer.

   What to expect:
   - The first step, **Installing packages**, prints nothing for **10–30 minutes** on a Zero W. In another SSH window, `top` shows `apt`, `dpkg` or `mkinitramfs` busy; that's normal. Don't press Ctrl-C.
   - Each step prints a green `==>` line. The install has worked when it ends with **`TeslaCam installed.`**, the *Next steps* list, the UI address and the `TeslaCam-Setup` hotspot name and password (also saved in `/etc/teslacam/config.toml`).
   - On the first install it asks for a reboot (USB gadget mode): `sudo reboot`.

   Re-running the installer is safe, after an update or an interrupted install: each step skips what is already done, and your config and clips are kept. You can override the drive size with `CAM_SIZE_GB=64`.

   > **The upload key** (`/home/teslacam/.ssh/id_ed25519`) lets the Pi log in to the NAS. `setup.sh` generates it automatically and prints its public part at the end. You add it to the NAS in step 3. To print it again: `sudo cat /home/teslacam/.ssh/id_ed25519.pub`. It is a different key from your Mac's login key in step 1.

3. **Set up the NAS and Tailscale.** For Synology DSM 7 there is a step-by-step guide: **[docs/nas-setup-synology.md](docs/nas-setup-synology.md)**. It covers Tailscale on both ends, key expiry, the shared folder and user, SSH/rsync, the firewall, installing the Pi's key, a test command and troubleshooting.
   On other NAS systems you need the same pieces: Tailscale, SSH with rsync, a user with write access to one folder, and the Pi's public key in that user's `~/.ssh/authorized_keys`.
4. Edit `/etc/teslacam/config.toml` on the Pi with `sudo nano /etc/teslacam/config.toml` (see [config.example.toml](install/config.example.toml)):
   - `[upload] host`: the NAS's Tailscale IP (`100.x.y.z`). `user` (`teslacam`) and `remote_dir` (`/volume1/TeslaCam`) are already filled in; change `remote_dir` if your shared folder is on another volume.
   - `[budget] monthly_gb` and `billing_day` (both can also be changed later on the UI's **Settings** page)
   - `[web] password_hash`: without it, anyone on the car hotspot can open the UI. This prompts for a password twice and writes its hash into the config:
     ```bash
     H=$(sudo -u teslacam env PYTHONPATH=/opt/teslacam python3 -m teslacam hash-password) && sudo sed -i "s|^password_hash = \".*\"|password_hash = \"$H\"|" /etc/teslacam/config.toml
     ```
     The hash is one line like `pbkdf2:sha256:150000$<salt>$<64 hex characters>`; it never contains the password itself.
5. Test the connection the same way the uploader checks it, then restart the services:
   ```bash
   sudo -u teslacam rsync --list-only -e "ssh -i /home/teslacam/.ssh/id_ed25519" teslacam@100.x.y.z:/volume1/TeslaCam/
   ```
   ```bash
   sudo systemctl restart teslacam-archiver teslacam-uploader teslacam-web
   ```
   It works if the listing appears without a password prompt. The UI's **Uploads** page then shows the NAS as *reachable* within about a minute. Right after a boot it may say *unreachable* until Tailscale has connected; it re-checks after 1, 2, 4… minutes, and shows the error under the badge if it stays down.
6. In the [Tailscale admin console](https://login.tailscale.com/admin/machines), **disable key expiry** for the Pi and the NAS. Otherwise uploads stop after 180 days.
7. On the UI's **Wifi** page, add the **car hotspot** while you're still on home wifi, so the Pi joins it in the car.
8. Shut the Pi down (`sudo shutdown now`) and plug it into the car. On the car's screen, check that the dashcam icon shows it recording. Trigger Sentry or tap the dashcam icon to save a clip; the clip appears in the UI within a few minutes.

On the NAS, clips land in `<remote_dir>/SentryClips/<event>/…` and `<remote_dir>/SavedClips/<event>/…`, in the same layout the car uses.

### Install troubleshooting

| What you see | What it means |
|---|---|
| No `TeslaCam-Setup` network after flashing | Expected: the hotspot only exists after `setup.sh`. Re-flash with your wifi under **Wireless LAN** (2.4 GHz, WPA2), or use a USB Ethernet adapter. |
| `ssh: … Connection refused` | The Pi is still on its first boot, or SSH wasn't enabled in Imager. Wait 5 minutes; otherwise create an empty file `ssh` on the `bootfs` partition (`touch /Volumes/bootfs/ssh`) and boot again. |
| `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!` | Your Mac remembers the key of an earlier install at that address. After a re-flash that's expected: `ssh-keygen -R <pi-ip>` and `ssh-keygen -R teslacam.local`, then connect again. |
| `perl: warning: Setting locale failed` / `setlocale: LC_CTYPE` | Harmless: macOS Terminal sends `LC_CTYPE=UTF-8`, which Linux doesn't know. Untick *Terminal → Settings → Profiles → Advanced → Set locale environment variables on startup*, or add `export LC_ALL=en_GB.UTF-8` to `~/.bashrc` on the Pi. |
| Installer output stops and the SSH window freezes | The connection dropped (see `tmux` above). If the Pi also restarted (`uptime` shows a few minutes), check `vcgencmd get_throttled`: anything other than `0x0` means low voltage, so use a better power supply. |
| `dpkg was interrupted, you must manually run 'sudo dpkg --configure -a'` | A package install was cut off. Run `sudo dpkg --configure -a`, then re-run the installer. |
| `nano: command not found` | You're probably in the NAS's shell, not the Pi's (Synology has no nano). The config file is on the Pi. |

Logs live in RAM, so they're gone after a restart. To keep them while you debug an unexpected restart:
```bash
sudo mkdir -p /etc/systemd/journald.conf.d /var/log/journal && printf '[Journal]\nStorage=persistent\n' | sudo tee /etc/systemd/journald.conf.d/zz-debug.conf && sudo systemctl restart systemd-journald
```
After the next restart, `journalctl -b -1 -n 60 --no-pager` shows the last messages before it. Delete `/etc/systemd/journald.conf.d/zz-debug.conf` when you're done, to spare the SD card.

## Wifi networks and the setup hotspot

> The setup hotspot is part of TeslaCam, so it only exists **after `setup.sh` has run** and the Pi has rebooted. Before that, the Pi only knows the wifi you entered in Raspberry Pi Imager. If you can't reach it on first boot, see the 2.4 GHz/WPA2 note in step 1 of the install.

The Pi remembers any number of wifi networks and joins whichever saved one is in range. Manage them on the UI's **Wifi** page, where you can scan, add, connect and forget networks.

If no saved network is reachable for **3 minutes** (or none is saved at all), the Pi starts its own network:

1. Join **`TeslaCam-Setup`** with the password the installer printed. It is also `network.setup_password` in `/etc/teslacam/config.toml`.
2. Most phones open the Wifi page by themselves (captive portal). If yours doesn't, browse to **http://10.42.0.1/wifi**. Your phone may warn "no internet"; choose to stay connected.
3. Pick the car hotspot from the list, which was scanned just before the setup network started, or type its name, enter the password and save.
4. The Pi leaves the setup network and joins your network. Switch your phone to the same network and open http://teslacam.local.

The Zero W can't run its own network and join another at the same time, so:
- uploads pause while the setup network is up, though recording and archiving continue;
- if nobody joins the setup network and the page goes unused for 5 minutes, the Pi goes back to trying the saved networks (they may simply have been out of range, e.g. the hotspot was off), then falls back again if there's still nothing. With no saved networks at all, it stays in setup mode.

Set `setup_hotspot = false` (or clear `setup_password`) under `[network]` to disable the setup network. The timings are configurable there too.

## Using the UI

Join the car hotspot with your phone and open **http://teslacam.local**. On Android, if `.local` names don't resolve, use the Pi's IP address from the router's client list.

- **Events:** thumbnails of every event, with filters for type, status, date and city. Select several to upload, protect or delete them at once.
- **Event:** a synced multi-camera player that opens 10 seconds before the trigger. Choose one camera, or **All** for a grid. Use space to play or pause and the arrow keys to skip ±5 s. You can also download single clips or the whole event as a .zip, upload now or re-upload, protect it, or delete it.
- **Uploads:** queue, progress, NAS reachability and data used in this billing period, plus pause/resume and retry-failed buttons.
- **System:** car-drive and SD usage, service health, logs, **Clean up now**, eject/attach the car drive, and reboot.
- **Wifi:** saved networks, scan and add, connect and forget, and the link status.
- **Settings:** data budget, billing day, speed limit and retention.

Deleting an event in the UI removes it from the Pi, and from the car drive at the next cleanup. Copies already on the NAS are never touched.

## Storage layout on the Pi

| Path | What |
|---|---|
| `/var/lib/teslacam/backingfiles.xfs` | Fully pre-allocated XFS volume (car-drive size + 4 GB), loop-mounted at `/backingfiles`. It holds `cam_disk.bin` and the short-lived snapshot. Because it is pre-allocated, the car can always write, even when the archive is full. |
| `/archive/events/` | Archived events, on the root filesystem. |
| `/archive/teslacam.db` | SQLite (WAL) database holding the event index, upload state, data usage and settings. |
| `/etc/teslacam/` | `config.toml` and `priv.env` |

Logs are kept in RAM only (`journalctl -u 'teslacam-*'`), which reduces SD wear and leaves nothing to corrupt when the car cuts power.

## Development (macOS/Linux)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```
```bash
.venv/bin/python -m pytest
```
```bash
dev/run_local.sh
```

`dev/run_local.sh` builds a fake car drive in `dev/data/car` with four events. On macOS it uses real H.264 clips generated by `dev/gen_clip.swift`. It then runs the archiver and uploader (the "NAS" is `dev/data/nas`, with uploads throttled so you can watch the queue) and serves the UI at <http://127.0.0.1:8765>. `car.mode = "directory"` stands in for the USB gadget.

```
teslacam/
  archiver.py   car drive → archive, car-drive cleanup
  uploader.py   archive → NAS (rsync), priority queue, budget stop
  meter.py      interface byte counters → monthly usage
  retention.py  local pruning rules
  car.py        ImageCar (gadget + reflink snapshots via priv.sh) / DirectoryCar (dev)
  tesla.py      TeslaCam folder / clip / event.json parsing
  db.py         SQLite schema, settings, status, UI→daemon requests
  web/          Flask app, templates, player.js
scripts/priv.sh root helper
install/        setup.sh, systemd units, config example
```

## Notes and limitations

- The setup hotspot needs NetworkManager (the default on Bookworm) plus a polkit rule that lets the `teslacam` user control it, which `setup.sh` installs. Some brcmfmac firmware versions are picky about WPA2 access-point mode. If phones can't join `TeslaCam-Setup`, check `journalctl -u teslacam-netwatch`.

- **Partly tested on real hardware.** `setup.sh`, Tailscale, the NAS connection and the UI have run on a Pi Zero W. The car side (the USB gadget in a Tesla, reflink snapshots and car-drive cleanup) hasn't been tried in a car yet. The Python services, the uploader and the UI are also covered by tests.
- **No hardware watchdog.** The Pi's watchdog chip allows at most 15 s, and on a Zero W a `systemctl daemon-reload` (run by installs and `apt`) blocks systemd for longer, so a systemd watchdog would reset the Pi, and pull the drive from the car. Earlier versions of `setup.sh` enabled one; re-running the installer removes it.
- A Zero W pushes roughly 3–6 MB/s through SSH inside WireGuard, which is CPU-bound but faster than a typical LTE uplink. Live playback in the UI is limited by the Zero W's 2.4 GHz wifi, so the player shows one camera by default.
- The Pi only boots once the car powers the USB port. Keep the Pi on a port that stays powered while Sentry is armed.
- Car-drive cleanup detaches the drive for a few seconds. It only runs when the drive is below the threshold and the car hasn't written for 90 s, but the car may briefly show a USB notice.
- RecentClips (the rolling dashcam buffer) are ignored on purpose.

## License

[MIT](LICENSE)
