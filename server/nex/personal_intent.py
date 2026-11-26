"""Explicit requests to read or change Nex's personal files."""

import re

from .conversation_intent import words
from .personal_files import safe_line
from .store import excerpt
from .wake import LEADING_WAKE

CHANGE = re.compile(r"^(remember that\b|remember\s*:|update (?:your )?soul\b|soul\.md\s*:)", re.IGNORECASE)
READ = {
    "what do you remember about me": "memory",
    "what do you know about me": "memory",
    "show me your memory": "memory",
    "read your memory file": "memory",
    "what is in your memory": "memory",
    "what is in your soul": "soul",
    "show me your soul": "soul",
    "read your soul file": "soul",
    "what is my current focus": "now",
    "what am i focused on": "now",
    "what do you think i am working on": "now",
}


def personal_intent(query):
    clean = LEADING_WAKE.sub("", query.strip(), count=1)
    if match := CHANGE.match(clean):
        name = "memory" if match[0].casefold().startswith("remember") else "soul"
        return "write", name, clean[match.end():].lstrip(" :,.!-")
    if name := READ.get(words(clean)):
        return "read", name, ""
    return None


def personal_reply(files, query, language, *, spoken=False):
    intent = personal_intent(query)
    if not intent:
        return None
    action, name, body = intent
    if action == "write":
        clean = safe_line(body, 500)
        if clean:
            heading = "## Saved on request" if name == "memory" else "## Owner preferences"
            files.append(name, clean, heading=heading)
            text = (f"I saved that in {name.upper()}.md." if language != "tr"
                    else f"Bunu {name.upper()}.md dosyasına kaydettim.")
        else:
            text = ("Tell me what you’d like me to save, or edit the file in Memory."
                    if language != "tr" else "Ne kaydetmemi istediğini söyle veya dosyayı düzenle.")
    else:
        markdown = files.snapshot()[name]["markdown"]
        lines = [line.removeprefix("- ").replace("**", "") for line in markdown.splitlines()
                 if line.strip() and not line.startswith(("#", "*20"))]
        text = excerpt("\n\n".join(lines), 620 if spoken else 4000).strip()
        if not text:
            text = ("I haven’t saved anything there yet." if language != "tr" else "Buraya henüz bir şey kaydetmedim.")
    return {"text": text, "language": language, "sources": [], "engine": "personal_context"}
