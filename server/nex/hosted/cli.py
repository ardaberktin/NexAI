"""Host-only account administration."""

import base64
import hashlib
import json
import ssl
import sys
from getpass import getpass

from ..network import CLIENT_PORT, TailnetEndpoint
from .auth import Accounts


def commands(parser):
    operations = parser.add_subparsers(dest="operation", required=True)
    for action in ("create", "reset-password", "disable", "enable"):
        command = operations.add_parser(action)
        command.add_argument("username")
        if action == "create":
            command.add_argument("--owner", action="store_true", help="attach the existing archive to this account")
    operations.add_parser("list")
    operations.add_parser("server-link")


def run(root, args):
    if not (root / "access.json").is_file():
        raise ValueError("Initialize the Nex archive first.")
    if args.operation == "server-link":
        endpoint = TailnetEndpoint.current(root)
        der = ssl.PEM_cert_to_DER_cert(endpoint.certificate.read_text())
        pin = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")
        print(f"nex://{endpoint.address}:{CLIENT_PORT}/{pin}")
        return
    accounts = Accounts(root)
    if args.operation == "list":
        print(json.dumps([{"username": user.username, "owner": user.owner, "enabled": enabled}
                          for user, enabled in accounts.list()], indent=2))
    elif args.operation in {"disable", "enable"}:
        accounts.set_enabled(args.username, args.operation == "enable")
    else:
        if not sys.stdin.isatty():
            raise ValueError("Run this command in a private interactive terminal.")
        password = getpass("Nex password: ")
        if password != getpass("Confirm password: "):
            raise ValueError("Passwords do not match.")
        if args.operation == "create":
            accounts.create(args.username, password, owner=args.owner)
        else:
            accounts.reset(args.username, password)
