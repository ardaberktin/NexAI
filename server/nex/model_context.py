"""Fit model calls to the active context window using the backend's chat tokenizer."""

import json
import logging
from time import monotonic

LOG = logging.getLogger(__name__)
INPUT_TOKENS_PATH = "/v1/chat/completions/input_tokens"
DEFAULT_CONTEXT = 12288
TEMPLATE_MARGIN = 8
LARGE_REPLY_FLOOR = 900
HISTORY_FIELDS = ("history", "recent", "earlier_answers", "conversation")
EVIDENCE_FIELDS = ("tools", "evidence", "cited_pages", "other_tools", "recalled_chat", "active_memory", "goal_state")
EXCERPT_FIELDS = {"body", "content", "excerpt", "preview", "text", "note"}
RECEIPT_TOOLS = {"clock", "create_pdf", "goal", "manage_goal", "save_memory", "weather_city"}
IMAGE_TOKEN_ESTIMATE = 512


class ContextLimit(ValueError):
    """The essential input and an adequate reply cannot fit."""


class ModelMessages(list):
    """A normal message list with optional decoding constraints for the model transport."""

    def __init__(self, messages, response_format=None, *, direct=False):
        super().__init__(messages)
        self.response_format = response_format
        self.direct = direct


def direct_generation(generate):
    """Decode fields and describe receipts directly after the model has interpreted the request."""
    async def direct(messages, tokens):
        marked = ModelMessages(messages, getattr(messages, "response_format", None), direct=True)
        return await generate(marked, tokens)
    return direct


def constrained_messages(messages, schema):
    return ModelMessages(messages, {"type": "json_schema", "json_schema": {"name": "nex_output", "schema": schema}})


def response_options(messages):
    value = getattr(messages, "response_format", None)
    return {"response_format": value} if value else {}


def input_tokens(payload):
    value = payload.get("input_tokens") if isinstance(payload, dict) else None
    return value if type(value) is int and value > 0 else None


def context_size(payload, default=DEFAULT_CONTEXT):
    config = payload.get("default_generation_settings") if isinstance(payload, dict) else None
    value = config.get("n_ctx") if isinstance(config, dict) else None
    return value if type(value) is int and value > 0 else default


def _content_tokens(content):
    if isinstance(content, str):
        return (len(content.encode("utf-8")) + 1) // 2
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                total += (len(part["text"].encode("utf-8")) + 1) // 2
            elif part.get("type") == "image_url":
                total += IMAGE_TOKEN_ESTIMATE
        return total
    return 0


def estimate_tokens(options):
    # Older relays may lack token counting. Keep an extra margin for their unknown template.
    return 48 + sum(8 + _content_tokens(item.get("content", "")) for item in options["messages"])


def log_completion(backend, started, payload, *, status="ok"):
    usage = payload.get("usage") if isinstance(payload, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    LOG.debug("Nex model completion backend=%s input_tokens=%s output_tokens=%s elapsed_ms=%d status=%s",
              backend, usage.get("prompt_tokens"), usage.get("completion_tokens"),
              (monotonic() - started) * 1000, status)


def shorten_excerpts(value, limit):
    if isinstance(value, list):
        return [shorten_excerpts(item, limit) for item in value]
    if isinstance(value, dict):
        return {
            key: item[:limit].rstrip() + "…" if key in EXCERPT_FIELDS and isinstance(item, str) and len(item) > limit
            else shorten_excerpts(item, limit)
            for key, item in value.items()
        }
    return value


def _compact_json_text(text, *, keep_history, evidence_limit=None):
    if not isinstance(text, str) or not text.startswith("{"):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    compact = dict(data)
    for key in HISTORY_FIELDS:
        if isinstance(compact.get(key), list):
            compact[key] = compact[key][-keep_history:] if keep_history else []
    if evidence_limit:
        for key in EVIDENCE_FIELDS:
            value = compact.get(key)
            if key == "tools" and isinstance(value, dict):
                compact[key] = {name: result if name in RECEIPT_TOOLS else shorten_excerpts(result, evidence_limit)
                                for name, result in value.items()}
            elif value is not None:
                compact[key] = shorten_excerpts(value, evidence_limit)
    if compact != data:
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    return text


def compact_prompt(options, *, keep_history, evidence_limit=None):
    messages = []
    for item in options["messages"]:
        content = item.get("content")
        if item.get("role") != "user":
            messages.append(item)
            continue
        if isinstance(content, str):
            updated = _compact_json_text(content, keep_history=keep_history, evidence_limit=evidence_limit)
            messages.append({**item, "content": updated} if updated != content else item)
        elif isinstance(content, list):
            parts = []
            changed = False
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    updated = _compact_json_text(part.get("text"), keep_history=keep_history,
                                                 evidence_limit=evidence_limit)
                    if updated != part.get("text"):
                        changed = True
                        parts.append({**part, "text": updated})
                        continue
                parts.append(part)
            messages.append({**item, "content": parts} if changed else item)
        else:
            messages.append(item)
    return {**options, "messages": messages}


class ContextBudget:
    def __init__(self, options, capacity):
        self.options = options
        self.capacity = capacity
        self.requested = options["max_tokens"]
        self.minimum = min(self.requested, LARGE_REPLY_FLOOR)
        self.retained = None

    def candidates(self):
        yield self.options
        previous = self.options
        for keep_history, evidence_limit in ((4, None), (2, None), (0, None), (0, 360), (0, 160)):
            candidate = compact_prompt(self.options, keep_history=keep_history, evidence_limit=evidence_limit)
            if candidate != previous:
                yield candidate
                previous = candidate

    def fit(self, options, count):
        actual = count if count is not None else estimate_tokens(options)
        if count is None and "response_format" in options:
            # Older relays can accept ordinary completions without the newer protocol fields.
            options = {key: value for key, value in options.items() if key != "response_format"}
        space = self.capacity - TEMPLATE_MARGIN - actual
        if space >= self.requested:
            return self.selected(options, actual, self.requested)
        if space >= self.minimum and self.retained is None:
            self.retained = (options, actual, space)
        if self.retained and self.retained[0]["messages"] != options["messages"]:
            # Keep the richer input rather than cutting source evidence to make the reply longer.
            return self.selected(*self.retained)
        return None

    def selected(self, options, count, limit):
        LOG.debug("Nex context input_tokens=%d reserve=%d capacity=%d compacted=%s",
                  count, limit, self.capacity, options["messages"] != self.options["messages"])
        return {**options, "max_tokens": limit}

    def remaining(self):
        if self.retained:
            return self.selected(*self.retained)
        LOG.warning("Nex model call exceeded the context budget capacity=%d", self.capacity)
        raise ContextLimit("The request and essential context exceed the local model's context window.")
