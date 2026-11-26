"""Read a small JSON reply to a live client request."""

import json


def control_payload(raw):
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError("Invalid voice control message")
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("Invalid voice control message")  # noqa: TRY004 - report malformed wire data uniformly.
    return message
