"""Generate a spoken acknowledgment from the public approval status alone."""

from ..goal_writing import json_object, model_messages

WRITE_APPROVAL_NOTICE = """You are Nex. Generate a brief spoken notice in the supplied language.
Explain that the request is waiting for approval and tell the user to open Nex to approve it.
Use only the supplied approval status. Keep the request generic and give no details about its contents.
Make clear that the request is still pending. Do not claim it was completed or promise automatic completion.
Return only a JSON object with a notice field containing one short sentence of plain text."""
NOTICE_SCHEMA = {
    "type": "object", "required": ["notice"], "additionalProperties": False,
    "properties": {"notice": {"type": "string", "minLength": 1, "maxLength": 240}},
}


async def with_approval_notice(result, generate):
    if not result.pop("approval_pending", False):
        return result
    status = {"status": "pending_approval", "next_action": "open_nex_to_approve", "language": result["language"]}
    raw = await generate(model_messages(WRITE_APPROVAL_NOTICE, status, schema=NOTICE_SCHEMA), 72)
    notice = json_object(raw).get("notice") if isinstance(raw, str) else None
    if isinstance(notice, str) and 0 < len(notice.strip()) <= 240:
        return result | {"approval_notice": " ".join(notice.split())}
    return result


def voice_reply_text(result):
    if result.get("engine") == "private_request":
        return result.get("approval_notice") or ""
    return result.get("text", "")
