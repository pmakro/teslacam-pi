# Setting up a Synology NAS (DSM 7) for TeslaCam

When you're done:

- the Pi in the car reaches the NAS at home through **Tailscale**, with no port forwarding and no public IP;
- it logs in over SSH as a dedicated user **`teslacam`**, using the key `setup.sh` generated, and can only write to one shared folder;
- clips land in `/volume1/TeslaCam/SentryClips/…` and `/volume1/TeslaCam/SavedClips/…`.

Allow about 20 minutes. Do it at home, with the Pi on your home wifi (or the car hotspot), and with a computer on the same network as the NAS.

> **Before you start:** run `sudo ./install/setup.sh` on the Pi. At the end it prints a public key that starts with `ssh-ed25519 AAAA…`. You'll need it in step 5. To print it again:
> `sudo cat /home/teslacam/.ssh/id_ed25519.pub`

---

## 1. Tailscale on the NAS

1. In DSM, open **Package Center**, search for **Tailscale** and click **Install**.
2. Open the **Tailscale** app from the main menu and click **Log in**. Sign in with Google, Microsoft, GitHub or Apple; this creates your tailnet (it's free for personal use).
3. Once it shows as connected, note the NAS's **Tailscale IP**, which looks like `100.x.y.z`. It's shown in the Tailscale app and at <https://login.tailscale.com/admin/machines>.

On DSM 7, Tailscale accepts **incoming** connections without any further setup, and that's all TeslaCam needs: the Pi connects to the NAS, never the other way round. Tailscale's docs describe a boot-up task (`tailscale configure-host`) that is only needed for **outgoing** connections from NAS apps, so you can skip it.

**Recommended: keep the NAS's Tailscale up to date.** Package Center updates lag behind. In **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**:
- General: user **root**, enabled
- Schedule: daily (e.g. 04:00)
- Task Settings → script: `tailscale update --yes`

## 2. Tailscale on the Pi

On the Pi (setup.sh already installed Tailscale):

```bash
sudo tailscale up --hostname=teslacam
```

It prints a `https://login.tailscale.com/a/…` link. Open it on your phone or computer, sign in with the **same account** as the NAS, and approve the device.

Check that the Pi can see the NAS:

```bash
tailscale ping 100.x.y.z
```

You should see `pong from …`. The first replies may say `via DERP`; that's fine, it just means the traffic is relayed.

## 3. Turn off key expiry for both devices

By default, Tailscale makes every device sign in again after 180 days. The Pi lives in the car and can't do that by itself, so when its key expired, uploads would silently stop.

At <https://login.tailscale.com/admin/machines>, for **both** `teslacam` and the NAS: click **⋯ → Disable key expiry**.

## 4. DSM: shared folder, user and services

**a) Shared folder.** **Control Panel → Shared Folder → Create**:
- Name: `TeslaCam`, on Volume 1. If you use another volume, adjust the path everywhere below (e.g. `/volume2/TeslaCam`).
- The Recycle Bin is optional. TeslaCam never deletes anything on the NAS.
- Skip encryption. Encrypted folders unmount on reboot until you unlock them, and uploads would fail meanwhile.

**b) User.** **Control Panel → User & Group → Create**:
- Name `teslacam`, with a long random password. You'll never type it; the Pi uses its key.
- Group: only `users`. **Don't** add it to `administrators` yet (see step 7).
- Shared folder permissions: **TeslaCam: Read/Write**, everything else: **No access**.
- Quota: optional, but a quota on TeslaCam is a good safety net.
- Application permissions: **Allow `rsync`** and **Deny** everything else (DSM, File Station, SMB, AFP, FTP, …).

**c) Services** in Control Panel:
- **Terminal & SNMP → Terminal →** ✅ *Enable SSH service*, port **22**
- **File Services → rsync →** ✅ *Enable rsync service* (SSH encryption port 22). This is what allows rsync over SSH for non-admin users.
- **User & Group → Advanced → User Home →** ✅ *Enable user home service*. The key in step 5 is stored in the user's home folder.

**d) Firewall and auto-block** in **Control Panel → Security**:
- If the **Firewall** is enabled, add a rule *Allow* with source **IP → Subnet `100.64.0.0` / mask `255.192.0.0`**. That's the whole Tailscale range, as Tailscale's Synology docs recommend.
- **Protection → Auto Block → Allow/Block List → Allow List → Create**: add the same subnet. Otherwise a few failed attempts while you're still setting things up can get the Pi's Tailscale IP blocked for good.

## 5. Install the Pi's key

You need a shell on the NAS once, as **your own admin account**. From a computer on your home network:

```bash
ssh your-admin-user@<NAS LAN IP>
```

Then paste these lines. Replace the `ssh-ed25519 AAAA…` line with the exact key the Pi printed, keeping the quotes:

```bash
KEY='ssh-ed25519 AAAA...paste...the...key... teslacam@teslacam'
H=/var/services/homes/teslacam
sudo mkdir -p "$H/.ssh"
echo "$KEY" | sudo tee "$H/.ssh/authorized_keys" >/dev/null
sudo chown -R teslacam:users "$H"
sudo chmod 700 "$H" "$H/.ssh"
sudo chmod 600 "$H/.ssh/authorized_keys"
sudo ls -la "$H/.ssh"
```

The last command should list `authorized_keys` owned by `teslacam`, with permissions `-rw-------`. SSH ignores the key if the home folder or `.ssh` is writable by anyone else, which is why the `chmod`s matter.

## 6. Configure and test from the Pi

Edit `/etc/teslacam/config.toml` on the Pi:

```toml
[upload]
host = "100.x.y.z"            # the NAS's Tailscale IP; works even if DNS is flaky on the hotspot
user = "teslacam"
remote_dir = "/volume1/TeslaCam"
```

Then run the same check the uploader runs:

```bash
sudo -u teslacam rsync --list-only -e "ssh -i /home/teslacam/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new" teslacam@100.x.y.z:/volume1/TeslaCam/
```

✅ **Success** means a line like `drwxrwxrwx … .`, with no password prompt.

Then restart the services:

```bash
sudo systemctl restart teslacam-uploader
```

The UI's **Uploads** page should show the NAS as *reachable* within a minute. It checks again every 15 minutes while there's nothing to upload, and before every upload.

## 7. If the test fails

| Message | Cause and fix |
|---|---|
| `teslacam@100.x.y.z's password:` (asks for a password) | The key wasn't accepted. Re-check step 5: an exact copy of the key on one line, owner `teslacam`, `700`/`600` permissions, and *User Home* enabled. |
| `Permission denied (publickey…)` or the connection closes right after login | DSM refused the login for a non-admin account. This differs between DSM 7 builds. Make sure **rsync** is allowed for the user and the **rsync service** is enabled (step 4). If it still fails, use the **fallback** below. |
| `rsync: … Permission denied` on `/volume1/TeslaCam` | The user doesn't have **Read/Write** on the TeslaCam shared folder. |
| `No such file or directory` | Wrong `remote_dir` (e.g. the share is on `/volume2`). |
| `Connection timed out` | Tailscale isn't connected on one side (`tailscale status` on the Pi), or the DSM firewall is blocking `100.64.0.0/10` (step 4d). |
| `Connection refused` | The SSH service is off, or uses a port other than 22. If you changed it, set `port = …` in `[upload]`. |

**Fallback if DSM won't let a non-admin user in:** add `teslacam` to the **administrators** group (**Control Panel → User & Group → teslacam → User Groups**). DSM always allows SSH for admins. It still can only *write* where it has permission, but an admin account can do more if the Pi were stolen. If you use this fallback, do the hardening below.

## 8. Recommended hardening

The Pi sits in a car, and a car can be stolen or broken into.

- **If the Pi goes missing:** remove it at <https://login.tailscale.com/admin/machines> and delete `/var/services/homes/teslacam/.ssh/authorized_keys` on the NAS. That cuts it off completely.
- **Restrict what the Pi can reach in your tailnet.** By default every device in a tailnet can reach every other device. In <https://login.tailscale.com/admin/acls>, tag the Pi and only let it talk to the NAS on SSH. Merge this into your policy file, keeping any rules you already have:

  ```jsonc
  {
    "tagOwners": { "tag:teslacam": ["autogroup:admin"] },
    "hosts":     { "nas": "100.x.y.z" },
    "acls": [
      // your own devices keep full access to each other:
      { "action": "accept", "src": ["autogroup:member"], "dst": ["*:*"] },
      // the car Pi may only reach the NAS's SSH port:
      { "action": "accept", "src": ["tag:teslacam"], "dst": ["nas:22"] }
    ]
  }
  ```

  Then re-register the Pi with the tag:

  ```bash
  sudo tailscale up --hostname=teslacam --advertise-tags=tag:teslacam --force-reauth
  ```

  Tagged devices have key expiry turned off automatically.

## What ends up on the NAS

```
/volume1/TeslaCam/
  SentryClips/2026-10-08_23-41-07/  2026-10-08_23-32-08-front.mp4 … event.json  thumb.png
  SavedClips/2026-10-06_18-20-12/   …
  .rsync-partial/                   (only while a file is half-uploaded)
```

TeslaCam only ever adds files to the NAS and never deletes from it. To keep the folder from growing forever, add a scheduled cleanup on the NAS, for example a Task Scheduler script running as `root`:

```bash
find /volume1/TeslaCam/SentryClips -mindepth 1 -maxdepth 1 -type d -mtime +90 -exec rm -rf {} +
```

That deletes Sentry events older than 90 days and keeps Saved clips.
