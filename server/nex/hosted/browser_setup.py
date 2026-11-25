"""First-owner setup over a Unix socket accessible to the host operator."""

import os
import socket
from socketserver import TCPServer

from ..http_api import API, WEB_DIR, Problem, Server
from ..privacy.state import AccessDenied
from .auth import LoginBusy
from .setup import create_browser_owner, owner_exists

WEB = {
    "/": ("setup.html", "text/html; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
    "/nex.css": ("nex.css", "text/css; charset=utf-8"),
}
HOST = "127.0.0.1:8768"
ORIGIN = f"http://{HOST}"


class BrowserSetup(API):
    def run(self, method):
        accounts = self.server.hosted.accounts
        try:
            if self.headers.get("Host") != HOST:
                raise Problem(403, f"Open {ORIGIN} in your browser.")
            if method == "GET" and self.path in WEB:
                name, content = WEB[self.path]
                self.send((WEB_DIR / name).read_bytes(), mime=content)
            elif (method, self.path) == ("GET", "/setup"):
                with accounts.connect() as db:
                    claimed = owner_exists(db)
                self.send({"owner_exists": claimed, "server_link": self.server.link if claimed else None})
            elif (method, self.path) == ("POST", "/setup"):
                if self.headers.get("Origin") != ORIGIN:
                    raise Problem(403, f"Submit the setup form from {ORIGIN}.")
                name = create_browser_owner(accounts, self.object(8_000), "browser-owner-setup")
                self.send({"username": name, "server_link": self.server.link}, 201)
            else:
                raise Problem(404, "This setup page was not found.")
        except Problem as exc:
            self.send({"error": exc.message}, exc.status)
        except AccessDenied as exc:
            self.send({"error": str(exc)}, 409)
        except LoginBusy:
            self.send({"error": "Too many setup requests. Try again shortly."}, 429)
        except (TypeError, ValueError) as exc:
            self.send({"error": str(exc)}, 400)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            pass


class BrowserSetupServer(Server):
    address_family = socket.AF_UNIX
    handler = BrowserSetup

    def __init__(self, runtime, hosted, link):
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = runtime.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("The Nex runtime directory must belong to the service user and have mode 700.")
        self.path = runtime / "setup.sock"
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_socket()):
            raise ValueError("The Nex setup socket path must be reserved for the service.")
        self.path.unlink(missing_ok=True)
        self.bound = False
        self.link = link
        super().__init__(str(self.path), None, admin=True, hosted=hosted)

    def server_bind(self):
        TCPServer.server_bind(self)
        self.bound = True
        self.path.chmod(0o600)

    def server_close(self):
        super().server_close()
        if self.bound:
            self.path.unlink(missing_ok=True)
            self.bound = False
