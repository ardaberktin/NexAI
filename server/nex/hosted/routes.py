"""Native app routes for account setup, profile and administration."""

import re

from ..privacy.keys import require_session
from .management import AccountSettings

USER = re.compile(r"/v2/auth/users/([0-9a-f]{32})(?:/(password|storage))?")


def account_request(api, hosted, method, path, problem):
    client = api.client_identity
    require_session(api.privacy, client, api.headers.get("X-Nex-Workspace", ""))
    settings = AccountSettings(hosted.accounts, client, api.client_address[0])
    route = method, path.removeprefix("/v2/auth/")
    if route == ("GET", "profile"):
        return settings.profile()
    if route == ("PUT", "profile"):
        return settings.rename(api.object(1_024))
    if route == ("PUT", "profile/password"):
        return settings.change_password(api.object(8_000))
    if route == ("POST", "profile/revoke-device"):
        return settings.revoke_device(api.object(1_024))
    if route == ("GET", "users"):
        return settings.users()
    if route == ("POST", "users"):
        return settings.create_user(api.object(8_000))
    if match := USER.fullmatch(path):
        identifier, action = match.groups()
        if method == "PUT" and action is None:
            return settings.update_user(identifier, api.object(1_024))
        if method == "PUT" and action == "password":
            return settings.reset_password(identifier, api.object(8_000))
        if action == "storage" and method in {"GET", "PUT"}:
            options = api.object(1_024) if method == "PUT" else None
            if options is not None and options.keys() != {"audio_limit_gib"}:
                raise problem(400, "Supply the audio storage limit.")
            return settings.storage(hosted, identifier, options)
    raise problem(404, "This account operation was not found.")
