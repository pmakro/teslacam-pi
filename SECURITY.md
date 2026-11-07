# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through GitHub instead:
**[Report a vulnerability](https://github.com/pmakro/teslacam-pi/security/advisories/new)** (the repo's *Security* tab → *Report a vulnerability*).

Include what you found, how to reproduce it, and what an attacker could do with it. This is a hobby project maintained in spare time: expect an acknowledgement within about a week, and a fix on `main` as soon as practical. You'll be credited in the advisory unless you prefer not to be.

## Supported versions

Only the latest `main` is supported. There are no releases or backports. To update a Pi: `cd ~/teslacam && git pull && sudo ./install/setup.sh`.

## Scope

In scope: everything in this repository, for example
- the web UI (`teslacam/web/`): authentication, CSRF, redirects, file and media access;
- the root helper `scripts/priv.sh` and its sudoers rule, and `install/setup.sh`;
- the archiver, uploader and network daemon, including how they call `rsync`, `ssh` and `nmcli`.

Out of scope: Tesla's software, Tailscale, Raspberry Pi OS packages and your NAS. Report those to their vendors. Problems that need someone who is already root on the Pi are out of scope too.

## How the project is meant to be secured

So reports can be judged against the intended design:
- The UI listens on the local network (the car hotspot) and should be protected with `web.password_hash`. It is not meant to be exposed to the internet.
- The services run as the unprivileged `teslacam` user. Root actions go only through the fixed list in `scripts/priv.sh`.
- The Pi reaches the NAS only over Tailscale, using a per-install SSH key, as a NAS user that can write to one folder. The [NAS guide](docs/nas-setup-synology.md) shows how to restrict the Pi in your tailnet to SSH on the NAS.
- Secrets (`secret_key`, `setup_password`, `password_hash`, SSH keys) are generated on the Pi and never belong in this repository.
