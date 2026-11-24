"""Resolve a live connection to the same account identity as the archive API."""

import asyncio
import ipaddress

from ..hosted.auth import authenticate_client, bearer
from ..network import TAILNET


def identity(scope, workspaces, *, loopback):
    peer = scope.get("client")
    if not peer or (not loopback and ipaddress.ip_address(peer[0]) not in TAILNET):
        return None, None
    headers = dict(scope["headers"])
    token = bearer(headers.get(b"authorization", b"").decode("latin1"))
    return authenticate_client(workspaces.accounts, workspaces.keys, token)


class LiveAccess:
    def __init__(self, app, *, workspaces, loopback):
        self.app, self.workspaces, self.loopback = app, workspaces, loopback

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        workspaces = self.workspaces()

        def resolve():
            return identity(scope, workspaces, loopback=self.loopback)

        role, client = await asyncio.to_thread(resolve)
        if role:
            state = await workspaces.workspace(client.account if client else None)
            if state is None:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1013})
                else:
                    await send({"type": "http.response.start", "status": 503,
                                "headers": [(b"cache-control", b"no-store")]})
                    await send({"type": "http.response.body", "body": b""})
                return
            scope["nex.workspace"] = state
            scope["nex.client"] = client
            scope["nex.role"] = role
        if "nex.role" not in scope or scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        handler = asyncio.create_task(self.app(scope, receive, send))
        monitor = asyncio.create_task(self.watch(resolve, (role, client)))
        try:
            completed, _ = await asyncio.wait((handler, monitor), return_when=asyncio.FIRST_COMPLETED)
            if handler in completed:
                await handler
            else:
                handler.cancel()
                await asyncio.gather(handler, return_exceptions=True)
                await send({"type": "websocket.close", "code": 1008})
        finally:
            handler.cancel()
            monitor.cancel()
            await asyncio.gather(handler, monitor, return_exceptions=True)

    @staticmethod
    async def watch(resolve, initial):
        while True:
            await asyncio.sleep(2)
            if await asyncio.to_thread(resolve) != initial:
                return


def authenticate(connection):
    return connection.scope.get("nex.role")


def workspace_for(connection):
    # Standalone route installations may supply their workspace directly.
    return connection.scope.get("nex.workspace", connection.app.state)
