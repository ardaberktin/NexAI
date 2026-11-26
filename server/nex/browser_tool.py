"""Sandboxed headless browser automation with structured DOM extraction fallback."""

import json
import os
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from urllib.parse import urljoin

from .store import excerpt
from .web_html import compact
from .web_transport import WebReadError, fetch, public_url

MAX_TEXT = 4_800
MAX_LINKS = 20
MAX_INTERACTIVE = 24
BROWSER_TIMEOUT = 18


class StructuredDOMParser(HTMLParser):
    """Extract compact readable text, headings, links, forms, and JSON-LD from HTML."""

    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.in_title = False
        self.in_json_ld = False
        self.title_parts = []
        self.text_parts = []
        self.headings = []
        self.links = []
        self.interactive = []
        self.structured = []
        self._current_heading = None
        self._current_link = None
        self._json_ld_buf = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = dict(attrs)
        if tag == "script" and (attr.get("type") or "").lower() == "application/ld+json":
            self.in_json_ld = True
            self._json_ld_buf = []
            self.skip_depth += 1
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._current_heading = []
        elif tag == "a" and (href := attr.get("href")):
            target = urljoin(self.base_url, href)
            try:
                safe = public_url(target)
                self._current_link = [safe, []]
            except (ValueError, WebReadError):
                self._current_link = None
        elif tag in {"button", "input", "select", "textarea"} and len(self.interactive) < MAX_INTERACTIVE:
            label = attr.get("aria-label") or attr.get("placeholder") or attr.get("name") or attr.get("value") or ""
            kind = attr.get("type") or tag
            if kind not in {"hidden"}:
                self.interactive.append({"tag": tag, "type": kind[:32], "label": compact(label)[:80]})

    def handle_data(self, data):
        if self.in_json_ld:
            self._json_ld_buf.append(data)
            return
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self._current_heading is not None:
            self._current_heading.append(data)
        if self._current_link is not None:
            self._current_link[1].append(data)
        if stripped := data.strip():
            self.text_parts.append(stripped)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "script" and self.in_json_ld:
            self.in_json_ld = False
            self.skip_depth = max(0, self.skip_depth - 1)
            raw = "".join(self._json_ld_buf).strip()
            if raw and len(self.structured) < 4:
                try:
                    parsed = json.loads(raw)
                    self.structured.append(parsed)
                except ValueError:
                    pass
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "title":
            self.in_title = False
        elif tag in {"h1", "h2", "h3"} and self._current_heading is not None:
            heading = compact("".join(self._current_heading))
            if heading and len(self.headings) < 16:
                self.headings.append(heading[:140])
            self._current_heading = None
        elif tag == "a" and self._current_link is not None:
            url, label_parts = self._current_link
            label = compact("".join(label_parts))
            if label and len(self.links) < MAX_LINKS:
                self.links.append({"text": label[:120], "url": url})
            self._current_link = None


def _find_chromium():
    for candidate in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if binary := shutil.which(candidate):
            return binary
    return None


def _render_dom_chromium(url):
    binary = _find_chromium()
    if not binary:
        return None
    with tempfile.TemporaryDirectory(prefix="nex-browser-") as profile:
        cmd = [
            binary, "--headless=new", "--disable-gpu", "--disable-extensions",
            "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
            "--mute-audio", "--hide-scrollbars", f"--user-data-dir={profile}",
            "--virtual-time-budget=4500", "--dump-dom", url,
        ]
        if os.path.isfile("/usr/bin/bwrap"):
            cmd = [
                "/usr/bin/bwrap", "--unshare-pid", "--unshare-ipc", "--new-session", "--die-with-parent",
                "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
                "--ro-bind-try", "/etc", "/etc", "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp", "--bind", profile, profile, "--", *cmd,
            ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=BROWSER_TIMEOUT, check=False,
            )
            if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 80:
                return proc.stdout[:1_200_000]
        except (OSError, subprocess.TimeoutExpired):
            return None
    return None


def inspect_page(url, *, action="open", query="", max_links=12):
    """Open a public HTTPS URL with headless Chromium when installed, else structured HTTP DOM fetch."""
    safe_url = public_url(url)
    rendered = _render_dom_chromium(safe_url)
    engine = "chromium_headless" if rendered is not None else "structured_http"
    html = rendered if rendered is not None else fetch(safe_url, timeout=10).content
    parser = StructuredDOMParser(safe_url)
    parser.feed(html)
    parser.close()
    title = compact("".join(parser.title_parts))[:180]
    body = compact(" ".join(parser.text_parts))
    if not body and not parser.links:
        raise WebReadError("The page exposed no readable text or links.")
    result = {
        "url": safe_url,
        "title": title,
        "engine": engine,
        "tainted": True,
        "headings": parser.headings[:10],
    }
    if action == "links":
        result["links"] = parser.links[:max(1, min(max_links, MAX_LINKS))]
    elif action == "search_page" and query:
        needle = query.strip().casefold()
        matches = []
        for sentence in body.split(". "):
            if needle in sentence.casefold():
                matches.append(sentence.strip()[:300])
                if len(matches) >= 8:
                    break
        result["matches"] = matches
        result["excerpt"] = excerpt(body, 1400)
    else:
        result["excerpt"] = excerpt(body, MAX_TEXT)
        result["links"] = parser.links[:min(8, max_links)]
        if parser.interactive:
            result["interactive"] = parser.interactive[:12]
    if parser.structured:
        result["structured_data"] = excerpt(json.dumps(parser.structured[:2], ensure_ascii=False), 900)
    return result
