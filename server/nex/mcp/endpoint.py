"""Resolve a configured MCP endpoint without forwarding credentials on redirects."""

import http.client
import ipaddress
import json
import re
from urllib.parse import urljoin, urlsplit

from ..web_transport import LOCAL_SUFFIXES, PinnedHTTPS, WebReadError, resolve_public
from . import MCPError

HEADER = re.compile(r"[!#$%&'*+.^_`|~\w-]{1,80}", re.ASCII)
RESERVED = {"accept", "accept-encoding", "connection", "content-encoding", "content-length", "content-type",
            "cookie2", "expect", "host", "last-event-id", "origin", "te", "trailer", "transfer-encoding", "upgrade",
            "user-agent"}


def headers_for(value):
    if not isinstance(value, dict) or len(value) > 12:
        raise MCPError("Supply up to 12 HTTP headers.")
    headers = {}
    for name, secret in value.items():
        if not isinstance(name, str) or not HEADER.fullmatch(name):
            raise MCPError("Enter a valid HTTP header name.")
        name = name.lower()
        if name in RESERVED or name.startswith(("mcp-", "proxy-", "sec-", "x-forwarded-")) or name in headers:
            raise MCPError("Use distinct authentication headers and leave transport headers to Nex.")
        if not isinstance(secret, str) or not re.fullmatch(r"[ -~]{1,4096}", secret) or not secret.strip():
            raise MCPError("Enter a nonempty HTTP header value on one line.")
        headers[name] = secret.strip()
    return headers


def endpoint_parts(value):
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]{1,2048}", value) or set(value) & {"\\", "#"}:
        raise MCPError("Enter a valid MCP server URL.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise MCPError("Enter a valid MCP server URL.") from None
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
            or port == 0 or "%" in parts.hostname):
        raise MCPError("Enter an MCP server URL without embedded credentials.")
    return parts


def origin(parts):
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def targets(root, url, parts):
    _, host, port = origin(parts)
    # Operators can share a loopback bridge with an individual account. The app cannot write this file.
    if host in {"127.0.0.1", "::1"}:
        try:
            allowed = json.loads((root / "mcp-loopback.json").read_text())
        except (OSError, ValueError):
            allowed = []
        if isinstance(allowed, list) and url in allowed:
            return [host]
        raise MCPError("Ask your Nex host operator to make this local MCP endpoint available to your account.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if parts.scheme != "https" or (address is None and ("." not in host or host.rstrip(".").endswith(LOCAL_SUFFIXES))):
        raise MCPError("Use a public HTTPS endpoint or an operator-configured loopback endpoint.")
    try:
        return resolve_public(host, port)
    except (OSError, WebReadError):
        raise MCPError("The MCP endpoint must resolve to a reachable public address.") from None


class Endpoint:
    def __init__(self, root, url, headers):
        self.parts = endpoint_parts(url)
        self.origin = origin(self.parts)
        self.addresses = targets(root, url, self.parts)
        self.headers = headers

    def target(self, location):
        if not location:
            return self.parts
        parts = endpoint_parts(urljoin(self.parts.geturl(), location))
        if origin(parts) != self.origin:
            raise MCPError("The MCP server advertised an endpoint on a different origin.")
        return parts

    def connect(self, address, timeout):
        scheme, host, port = self.origin
        if scheme == "http":
            return http.client.HTTPConnection(address, port=port, timeout=timeout)
        return PinnedHTTPS(host, address, timeout, port=port)
