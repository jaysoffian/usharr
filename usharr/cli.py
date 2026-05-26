"""Command-line entry point.

Subcommands:
    auth              Link this usharr to a Plex account (PIN OAuth).
    auth --status     Show current link state.
    auth --reset      Forget the stored Plex token and server.
"""

import argparse
import asyncio
import sys

from usharr import db, plex
from usharr.config import get_config


async def cmd_auth(args: argparse.Namespace) -> int:
    get_config()
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

    p_auth = sub.add_parser("auth", help="link to a Plex account via PIN OAuth")
    p_auth.add_argument("--status", action="store_true", help="show link state")
    p_auth.add_argument("--reset", action="store_true", help="forget stored token")
    p_auth.set_defaults(func=cmd_auth)

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
