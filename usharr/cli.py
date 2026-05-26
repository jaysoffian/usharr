"""Command-line entry point.

Subcommands:
    probe PATH        Probe a file and print JSON; does not touch the DB.
    scan              Run one full-scan pass (reconcile + probe) and exit.
    get PATH          Print the DB row for PATH, if any.
    auth              Link this usharr to a Plex account (PIN OAuth).
    auth --status     Show current link state.
    auth --reset      Forget the stored Plex token and server.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from usharr import db, plex, scanner
from usharr.ardetector import detect
from usharr.config import load_config


async def cmd_probe(args: argparse.Namespace) -> int:
    load_config()
    logging.basicConfig(
        level=logging.DEBUG,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        result = await detect(Path(args.path))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), indent=2))
    return 0


async def cmd_scan(_: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    db.init_db()
    workers = [
        asyncio.create_task(scanner.mediainfo_worker()),
        asyncio.create_task(scanner.ardetector_worker()),
    ]
    try:
        await scanner.scan_and_drain()
    finally:
        for w in workers:
            w.cancel()
        for w in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w
        db.close_db()
    return 0


async def cmd_get(args: argparse.Namespace) -> int:
    load_config()
    db.init_db()
    try:
        row = db.get(args.path)
        if row is None:
            print(json.dumps({"error": f"no record for {args.path}"}), file=sys.stderr)
            return 1
        out = asdict(row)
        mi = db.get_mediainfo(args.path)
        ar = db.get_ardetector(args.path)
        out["mediainfo"] = asdict(mi) if mi else None
        if ar is None:
            out["ardetector"] = None
        else:
            ar_out = asdict(ar)
            if ar_out["aspect_samples"]:
                ar_out["aspect_samples"] = json.loads(ar_out["aspect_samples"])
            out["ardetector"] = ar_out
        out["audio"] = [asdict(t) for t in db.get_audio_tracks(args.path)]
        out["subtitle"] = [asdict(t) for t in db.get_subtitle_tracks(args.path)]
    finally:
        db.close_db()
    print(json.dumps(out, indent=2))
    return 0


async def cmd_auth(args: argparse.Namespace) -> int:
    load_config()
    db.init_db()
    try:
        if args.status:
            return auth_status()
        if args.reset:
            plex.clear_auth()
            print("Cleared stored Plex credentials.")
            return 0
        return await auth_link()
    finally:
        db.close_db()


def auth_status() -> int:
    try:
        token, url, name = plex.load_auth()
    except plex.PlexNotLinkedError as exc:
        print(str(exc))
        return 1
    print(f"Linked to {name} at {url}")
    print(f"Token: {token[:4]}…{token[-4:]}")
    return 0


async def auth_link() -> int:
    client_id = plex.get_or_create_client_id()
    pin = await plex.create_pin(client_id)
    print("Open this URL in a browser and click Allow:")
    print(f"  {plex.auth_url(client_id, pin.code)}")
    print(f"Or visit https://plex.tv/link and enter code: {pin.code}")
    print("Waiting for authorization (5 min timeout)…")

    token = await plex.poll_pin(client_id, pin.id, timeout=300.0)
    if token is None:
        print("Timed out waiting for authorization.", file=sys.stderr)
        return 1

    email = await plex.account_email(client_id, token)
    if email:
        print(f"Authorized as {email}.")

    url, name = await plex.discover_server(client_id, token)
    plex.save_auth(token, url, name)
    print(f"Discovered server: {name} at {url}")
    print("Saved credentials to /config/usharr.db.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usharr")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser("probe", help="probe a file; print JSON; no DB writes")
    p_probe.add_argument("path")
    p_probe.set_defaults(func=cmd_probe)

    p_scan = sub.add_parser("scan", help="run one full-scan pass and exit")
    p_scan.set_defaults(func=cmd_scan)

    p_get = sub.add_parser("get", help="print the DB row for PATH")
    p_get.add_argument("path")
    p_get.set_defaults(func=cmd_get)

    p_auth = sub.add_parser("auth", help="link to a Plex account via PIN OAuth")
    p_auth.add_argument("--status", action="store_true", help="show link state")
    p_auth.add_argument("--reset", action="store_true", help="forget stored token")
    p_auth.set_defaults(func=cmd_auth)

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
