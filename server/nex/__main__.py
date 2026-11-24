import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import os
import signal
import socket
import ssl
import threading
from pathlib import Path

from .accounts.credentials import Credentials
from .accounts.http import AccountError
from .accounts.setup import authorize
from .hosted import cli as account_cli
from .hosted.auth import Accounts
from .hosted.workspaces import HostedWorkspaces
from .http_api import Server
from .initialize import initialize
from .network import CLIENT_PORT, TAILNET, TAILSCALE_INTERFACE, TailnetEndpoint
from .privacy.keys import issue_setup
from .privacy.state import PrivacyState

logger = logging.getLogger(__name__)


def read_access(root):
    return json.loads((root / "access.json").read_text())


def pairing(root, device):
    credentials = read_access(root)
    if device == "admin":
        print(credentials["keys"]["admin"])
        return
    if not Accounts(root).legacy_allowed():
        raise ValueError("Sign in with your Nex account. Legacy pairing is disabled.")
    endpoint = TailnetEndpoint.current(root)
    certificate = ssl.PEM_cert_to_DER_cert(endpoint.certificate.read_text())
    digest = base64.b64encode(hashlib.sha256(certificate).digest()).decode()
    print(json.dumps({"url": f"https://{endpoint.address}:{CLIENT_PORT}",
                      "pin": digest, "token": credentials["keys"][device], "device": device,
                      "setup": issue_setup(PrivacyState(root), device)}))


def report_ready():
    destination = os.environ.get("NOTIFY_SOCKET")
    if not destination:
        return
    if destination.startswith("@"):
        destination = "\0" + destination[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
        notification.connect(destination)
        notification.sendall(b"READY=1")


def serve(root, runtime, ssd_audio=None):
    endpoint = TailnetEndpoint.current(root)
    credentials = read_access(root)
    if ssd_audio is not None:
        ssd_audio = ssd_audio.resolve()
        ssd_audio.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_dev == ssd_audio.stat().st_dev:
            raise ValueError("The SSD audio directory and data archive must be on separate drives.")
    ending = threading.Event()
    hosted = HostedWorkspaces(root, runtime, credentials, ending, ssd_audio=ssd_audio)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(endpoint.certificate, endpoint.key)
    ingress = Server((endpoint.address, CLIENT_PORT), hosted.owner, tls=context, interface=TAILSCALE_INTERFACE,
                     allow_peer=lambda addr: ipaddress.IPv4Address(addr) in TAILNET, hosted=hosted)
    console = Server(("127.0.0.1", 8766), hosted.owner, admin=True, hosted=hosted)

    def stop(*_):
        ending.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    hosted.start()
    for server in (ingress, console):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    report_ready()
    logger.info("Nex is serving its archive on Tailscale, with a loopback console.")
    ending.wait()
    for server in (ingress, console):
        server.shutdown()
        server.server_close()
    hosted.close()


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Nex offline assistant")
    parser.add_argument("--data", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)
    server = subcommands.add_parser("serve")
    server.add_argument("--runtime", type=Path, required=True)
    server.add_argument("--ssd-audio", type=Path)
    export = subcommands.add_parser("pair")
    export.add_argument("device", choices=("mac", "iphone", "admin"))
    init = subcommands.add_parser("init", help="initialize a private server archive")
    init.add_argument("--audio-quota-gib", type=int, default=48)
    init.add_argument("--min-free-gib", type=int, default=2)
    connect = subcommands.add_parser("connect")
    connect.add_argument("provider", choices=("google", "ticktick"))
    connect.add_argument("--client", type=Path)
    connect.add_argument("--port", type=int, default=8627)
    subcommands.add_parser("connections")
    account_cli.commands(subcommands.add_parser("account", help="manage hosted Nex accounts"))
    subcommands.add_parser("provision-tailnet")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.data.resolve(), args.runtime.resolve(), args.ssd_audio)
    elif args.command == "init":
        try:
            initialize(args.data.resolve(), args.audio_quota_gib, args.min_free_gib)
        except (OSError, ValueError) as exc:
            parser.exit(1, str(exc) + "\n")
        print(f"Nex archive initialized at {args.data.resolve()}")
    elif args.command in {"pair", "account"}:
        try:
            if args.command == "pair":
                pairing(args.data.resolve(), args.device)
            else:
                account_cli.run(args.data.resolve(), args)
        except (OSError, ValueError) as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "connect":
        if not 1024 <= args.port <= 65535 or (args.provider == "google" and not args.client):
            parser.error("Choose an unprivileged port and pass --client for Google.")
        try:
            authorize(args.data, args.provider, args.port, args.client)
        except (AccountError, OSError) as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "connections":
        print(json.dumps(Credentials(args.data).status(), indent=2))
    else:
        endpoint = TailnetEndpoint.current(args.data.resolve())
        endpoint.provision()
        print(f"Nex client endpoint: https://{endpoint.address}:{CLIENT_PORT}")


if __name__ == "__main__":
    main()
