"""Build provider OAuth flows and support the legacy loopback sign-in command."""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlsplit

from .credentials import (
    SCOPES,
    TOKEN_URLS,
    WRITE_SCOPES,
    Credentials,
    has_scopes,
)
from .http import AccountError, request

AUTHORIZE = {"google": "https://accounts.google.com/o/oauth2/v2/auth",
             "ticktick": "https://ticktick.com/oauth/authorize"}
TICK_RESOURCE = "https://mcp.ticktick.com/"
TICK_REDIRECT = "com.ardaberktin.nex.oauth:/callback"


@dataclass(frozen=True)
class Authorization:
    provider: str
    client: dict
    redirect: str
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    writes: dict = field(default_factory=dict)

    @property
    def scopes(self):
        return SCOPES[self.provider] | {WRITE_SCOPES[key] for key in self.writes}

    def url(self):
        challenge = hashlib.sha256(self.verifier.encode()).digest()
        params = {
            "client_id": self.client["client_id"], "redirect_uri": self.redirect, "response_type": "code",
            "scope": " ".join(sorted(self.scopes)), "state": self.state,
            "code_challenge": base64.urlsafe_b64encode(challenge).decode().rstrip("="),
            "code_challenge_method": "S256",
        }
        if self.provider == "google":
            params |= {"access_type": "offline", "prompt": "consent"}
        else:
            params["resource"] = TICK_RESOURCE
        return AUTHORIZE[self.provider] + "?" + urlencode(params)

    def save(self, vault, code):
        form = self.client | {"grant_type": "authorization_code", "code": code,
                              "redirect_uri": self.redirect, "code_verifier": self.verifier}
        if self.provider == "ticktick":
            form["resource"] = TICK_RESOURCE
        raw, _ = request(TOKEN_URLS[self.provider], form=form)
        record = vault.accept(self.provider, self.client, raw, requested=self.scopes)
        if self.writes and not has_scopes(record, SCOPES[self.provider] | {WRITE_SCOPES[key] for key in self.writes}):
            raise AccountError("The provider did not grant all requested account permissions. The existing connection was kept.")
        record["write_settings"] = self.writes
        with vault.locked(self.provider):
            vault.write(self.provider, record)


def google_client(path):
    try:
        installed = json.loads(path.read_text())["installed"]
        return {key: installed[key] for key in ("client_id", "client_secret")}
    except (OSError, TypeError, ValueError, KeyError, AttributeError):
        raise AccountError("Pass the downloaded Google Desktop OAuth client JSON with --client.") from None


def ticktick_client(redirect, *, write=False):
    response, _ = request("https://api.ticktick.com/oauth/register", payload={
        "client_name": "Nex",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "tasks:read tasks:write" if write else "tasks:read",
    })
    if not isinstance(response.get("client_id"), str):
        raise AccountError("TickTick did not register the Nex OAuth client.")
    return {"client_id": response["client_id"]}


class Callback(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        values = parse_qs(url.query, max_num_fields=16)
        received = values.get("state", [""])[0]
        if url.path != "/callback" or not hmac.compare_digest(received.encode(), self.server.state.encode()):
            self.send_error(400, "Invalid authorization callback")
            return
        self.server.answer = values.get("code", [""])[0]
        body = b"Return to your terminal to finish the Nex connection."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)


def authorize(root, provider, port, config=None):
    with HTTPServer(("127.0.0.1", port), Callback) as listener:
        redirect = f"http://127.0.0.1:{port}/callback"
        client = google_client(config) if provider == "google" else ticktick_client(redirect)
        flow = Authorization(provider, client, redirect)
        listener.state, listener.answer, listener.timeout = flow.state, None, 1
        print("Open this URL in the browser on the Mac with the SSH forward active:", flush=True)
        print(flow.url(), flush=True)
        deadline = monotonic() + 600
        while listener.answer is None and monotonic() < deadline:
            listener.handle_request()
        if not listener.answer:
            raise AccountError("Authorization was declined or expired.")
    flow.save(Credentials(root), listener.answer)
    print(provider + " authorized.", flush=True)
