"""Read bounded, read-only Gmail evidence for goal checks."""

import base64
from binascii import Error as Base64Error
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

from .credentials import GMAIL
from .google import GMAIL_URL, INVISIBLE_PADDING, clean, get, gmail_ids
from .http import AccountError, AccountHTTPError

BODY_FIELDS = "mimeType,filename,body(data)"
for _ in range(3):
    BODY_FIELDS = f"mimeType,filename,body(data),parts({BODY_FIELDS})"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if tag in ("head", "script", "style"):
            self.hidden = tag

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text_parts(part):
    if part.get("filename"):
        return
    kind, data = part.get("mimeType"), part.get("body", {}).get("data")
    if kind in {"text/plain", "text/html"} and isinstance(data, str):
        prefix = data[:16_384]
        try:
            text = base64.urlsafe_b64decode(prefix + "=" * (-len(prefix) % 4)).decode("utf-8", errors="replace")
        except Base64Error:
            return
        if kind == "text/html":
            parser = VisibleText()
            parser.feed(text)
            text = " ".join(parser.parts)
        if excerpt := clean(text.translate(INVISIBLE_PADDING), 1_800):
            yield kind, excerpt
    for child in part.get("parts", []):
        yield from text_parts(child)


def message_ids(token, since, until, phrase):
    query = f"after:{int(since.timestamp()) - 1} before:{int(until.timestamp()) + 1}"
    query += f' in:inbox "{phrase}"' if phrase else " -in:sent -in:drafts"
    params = {"q": query, "maxResults": 100, "fields": "messages(id),nextPageToken"}
    pages = set()
    while True:
        result = get(GMAIL_URL, token, params)
        yield from (message["id"] for message in result.get("messages", []))
        page = result.get("nextPageToken")
        if not page:
            return
        if page in pages:
            raise AccountError("Gmail returned an invalid page token.")
        pages.add(page)
        params["pageToken"] = page


def message(token, identifier):
    url = GMAIL_URL + "/" + quote(identifier, safe="")
    params = {"format": "metadata", "metadataHeaders": ["From", "Date", "Subject"],
              "fields": "internalDate,snippet,payload(headers)"}
    try:
        try:
            item = get(url, token, {"format": "full", "fields": f"internalDate,snippet,payload(headers,{BODY_FIELDS})"})
        except AccountError as exc:
            if str(exc) != "The account response was too large.":
                raise
            item = get(url, token, params)
    except AccountHTTPError as exc:
        if exc.status == 404:
            return None
        raise
    payload = item.get("payload", {})
    headers = {header["name"].casefold(): header.get("value") for header in payload.get("headers", [])}
    title = clean(headers.get("subject"), 140) or "No subject"
    snippet = clean(unescape(item.get("snippet") or "").translate(INVISIBLE_PADDING), 350)
    parts = list(text_parts(payload))
    body = next((value for kind, value in parts if kind == "text/plain"), parts[0][1] if parts else "")
    return int(item["internalDate"]), {"subject": title, "sender": clean(headers.get("from"), 120),
                                      "sent_at": clean(headers.get("date"), 64), "preview": snippet, "body": body}


def detailed_inbox(vault, intent):
    token = vault.token("google", {GMAIL})
    identifiers = gmail_ids(token, intent, 12 if intent.priority else 8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [result[1] for result in pool.map(lambda identifier: message(token, identifier), identifiers)
                if result is not None]
