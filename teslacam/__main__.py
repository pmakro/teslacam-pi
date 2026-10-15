"""Entry point: python -m teslacam <command>"""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys

from . import config as config_mod


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="teslacam")
    parser.add_argument("-c", "--config", help="config file (default: $TESLACAM_CONFIG or /etc/teslacam/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("archiver", help="run the archiver daemon")
    sub.add_parser("uploader", help="run the uploader daemon")
    sub.add_parser("web", help="run the web UI")
    sub.add_parser("netwatch", help="run the wifi watchdog")
    sub.add_parser("init-db", help="create or migrate the database")
    sub.add_parser("archive-once", help="run a single archiver cycle")
    sub.add_parser("hash-password", help="print a password hash for web.password_hash")
    sub.add_parser("secret-key", help="print a random value for web.secret_key")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.command == "hash-password":
        from werkzeug.security import generate_password_hash

        pw = getpass.getpass("UI password: ")
        if pw != getpass.getpass("Repeat: "):
            print("passwords do not match", file=sys.stderr)
            return 1
        # pbkdf2 rather than werkzeug's scrypt default: cheaper on a Pi Zero and always available.
        print(generate_password_hash(pw, method="pbkdf2:sha256:150000"))
        return 0
    if args.command == "secret-key":
        print(secrets.token_hex(32))
        return 0

    cfg = config_mod.load(args.config)

    if args.command == "init-db":
        from . import db

        db.open_db(cfg).close()
        print(f"database ready at {cfg.db_path}")
    elif args.command == "archiver":
        from . import archiver

        archiver.run(cfg)
    elif args.command == "archive-once":
        from . import archiver, db
        from .car import from_config

        archiver.cycle(db.open_db(cfg), cfg, from_config(cfg))
    elif args.command == "uploader":
        from . import uploader

        uploader.run(cfg)
    elif args.command == "netwatch":
        from . import netwatch

        netwatch.run(cfg)
    elif args.command == "web":
        from .web.app import serve

        serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
