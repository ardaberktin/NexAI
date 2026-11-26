"""A bounded HTTPS client for account reads and OAuth grants."""

import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

HOSTS = {"gmail.googleapis.com", "www.googleapis.com", "oauth2.googleapis.com",
         "api.ticktick.com", "mcp.ticktick.com"}
MAX_BODY = 2_000_000
TOKEN = re.compile(r"[\x21-\x7e]{1,8192}")


class AccountError(Exception):
    pass


class AccountHTTPError(AccountError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"The provider returned HTTP {status}.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def request(url, *, token=None, form=None, payload=None, headers=None):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in HOSTS:
        raise AccountError("The account endpoint is unavailable.")
    options = {
        "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "NexAccounts/1.0",
        **(headers or {}),
    }
    if token:
        if not TOKEN.fullmatch(token):
            raise AccountError("Reconnect this account.")
        options["Authorization"] = "Bearer " + token
    body = None
    if form is not None:
        body = urlencode(form).encode()
        options["Content-Type"] = "application/x-www-form-urlencoded"
    if payload is not None:
        body = json.dumps(payload).encode()
        options["Content-Type"] = "application/json"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    outgoing = Request(url, data=body, headers=options, method="GET" if body is None else "POST")
    try:
        with opener.open(outgoing, timeout=9) as response:
            kind = response.headers.get_content_type()
            if kind == "text/event-stream":
                data = read_event(response)
            else:
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise AccountError("The account response was too large.")
            return (json.loads(data) if data else {}), response.headers
    except HTTPError as error:
        raise AccountHTTPError(error.code) from None
    except (OSError, UnicodeError, ValueError):
        raise AccountError("The account service could not be read.") from None


def read_event(response):
    lines, remaining = [], MAX_BODY
    while remaining > 0:
        line = response.readline(remaining + 1)
        if not line:
            break
        remaining -= len(line)
        if line.startswith(b"data:"):
            lines.append(line[5:].strip())
        if not line.strip() and lines:
            data = b"\n".join(lines)
            if b'"id"' in data:
                return data
            lines.clear()
    raise AccountError("The account service did not return a complete response.")
