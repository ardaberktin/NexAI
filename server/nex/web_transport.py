"""Public, cookie-free HTTPS reads with a pinned address at every redirect."""

import http.client
import ipaddress
import re
import socket
import ssl
import threading
from dataclasses import dataclass
from time import monotonic
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

SEARCH_ROUTES = {("lite.duckduckgo.com", "/lite/"), ("www.bing.com", "/search"), ("www.bing.com", "/news/search")}
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
ACTION = re.compile(
    r"(?:^|/)(?:account|admin|api|auth|authorize|callback|cart|checkout|confirm|delete|download|"
    r"edit|log[-_]?in|log[-_]?out|oauth|pay|payment|purchase|redeem|register|reset|session|settings|"
    r"sign[-_]?in|sign[-_]?out|sign[-_]?up|submit|subscribe|token|unsubscribe|upload|verify|scl)"
    r"(?:\.(?:html?|php|cgi|aspx?))?(?:$|/)",
    re.IGNORECASE,
)
OPAQUE = re.compile(r"^(?=[\w-]{26,}$)(?=.*[A-Z])(?=.*[0-9])[A-Za-z0-9_-]+$")
LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".ts.net", ".onion", ".arpa", ".test")
NAT64_RANGES = tuple(ipaddress.ip_network(value) for value in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))
HTML_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
FEED_TYPES = {"text/xml", "application/xml", "application/rss+xml"}
MAX_BODY = 780_000
REDIRECTS = {301, 302, 303, 307, 308}
TLS = ssl.create_default_context()
SLOTS = threading.BoundedSemaphore(4)


class WebReadError(Exception):
    pass


@dataclass(frozen=True)
class Document:
    url: str
    content: str
    content_type: str


def public_url(value, *, search=False):
    if not isinstance(value, str) or len(value) > 1900 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise WebReadError("This address isn’t a public HTTPS page.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").removesuffix(".").encode("idna").decode().lower()
        path = unquote(parts.path or "/", errors="strict")
        port = parts.port
    except (UnicodeError, ValueError):
        raise WebReadError("This address isn’t a public HTTPS page.") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or port not in (None, 443) or not HOST.fullmatch(host) or host.endswith(LOCAL_SUFFIXES)
            or re.search(r"[\x00-\x1f\x7f\\%?#@]", path) or "//" in path
            or any(segment in {".", ".."} for segment in path.split("/"))):
        raise WebReadError("This address isn’t a public HTTPS page.")
    if search:
        if (host, path) not in SEARCH_ROUTES:
            raise WebReadError("The search provider redirected to a different page.")
    elif (ACTION.search(path) or ACTION.search(host.partition(".")[0])
          or any(OPAQUE.fullmatch(segment) for segment in path.split("/"))):
        raise WebReadError("Nex can only open public reading pages.")
    return urlunsplit(("https", host, quote(path, safe="/!$&'()*+,-._~;=:"), parts.query if search else "", ""))


def routable(value):
    address = ipaddress.ip_address(value)
    if address.version == 6 and (address.ipv4_mapped or any(address in network for network in NAT64_RANGES)):
        return False
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_loopback)


def resolve_public(host, port=443):
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or not all(routable(address) for address in addresses):
        raise WebReadError("That address points to a private or reserved network.")
    return sorted(addresses, key=lambda address: ":" in address)


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout, *, port=443):
        super().__init__(host, port=port, timeout=timeout, context=TLS)
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = TLS.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def remaining(deadline):
    duration = deadline - monotonic()
    if duration <= 0:
        raise WebReadError("The public page took too long to respond.")
    return min(duration, 4)


def open_response(parts, deadline, *, accept="text/html, application/xhtml+xml, text/plain, application/rss+xml"):
    for address in resolve_public(parts.hostname):
        connection = PinnedHTTPS(parts.hostname, address, remaining(deadline))
        try:
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", target, headers={
                "User-Agent": "NexReadOnly/1.0",
                "Accept": accept,
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            return connection, connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
    raise WebReadError("The public site couldn’t be reached over HTTPS.")


def body_text(response, connection, deadline):
    content = bytearray()
    while len(content) < MAX_BODY:
        active_socket = connection.sock or getattr(getattr(response.fp, "raw", None), "_sock", None)
        if active_socket is not None:
            active_socket.settimeout(remaining(deadline))
        else:
            remaining(deadline)
        chunk = response.read1(min(32_768, MAX_BODY - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    encoding = response.headers.get_content_charset() or "utf-8"
    try:
        return bytes(content).decode(encoding, errors="replace")
    except LookupError:
        return bytes(content).decode("utf-8", errors="replace")


def fetch(url, *, search=False, feed=False, timeout=6):
    if not SLOTS.acquire(timeout=1):
        raise WebReadError("Nex is already reading several pages.")
    try:
        return fetch_public(url, search, monotonic() + timeout, feed=feed)
    except (OSError, http.client.HTTPException) as exc:
        raise WebReadError("The public site couldn’t be read.") from exc
    finally:
        SLOTS.release()


def fetch_public(url, search, deadline, *, feed=False):
    for _ in range(5):
        url = public_url(url, search=search)
        connection, response = open_response(urlsplit(url), deadline)
        try:
            if response.status in REDIRECTS and (location := response.headers.get("Location")):
                if search:
                    raise WebReadError("The search provider redirected unexpectedly.")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise WebReadError("The public site did not return a readable page.")
            content_type = response.headers.get_content_type()
            permitted = HTML_TYPES | FEED_TYPES if search or feed else HTML_TYPES
            if (content_type not in permitted or response.headers.get("Content-Encoding", "identity") != "identity"
                    or response.headers.get("Content-Disposition", "").lower().startswith("attachment")):
                raise WebReadError("Nex can read public text pages only.")
            return Document(url, body_text(response, connection, deadline), content_type)
        finally:
            response.close()
            connection.close()
    raise WebReadError("The public page redirected too many times.")
