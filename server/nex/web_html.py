"""Extract text and static links from a public page without executing it."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .web_transport import WebReadError, public_url

BLOCKED = {"script", "style", "noscript", "iframe", "object", "embed", "svg", "template", "form", "button",
           "select", "textarea", "header", "footer", "aside", "nav"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BREAKS = {"article", "blockquote", "br", "dd", "div", "dt", "h1", "h2", "h3", "h4", "h5", "li", "p", "section", "tr"}
HIDDEN = {"hidden", "inert", "disabled"}
LINK_SKIP = {"cookie preferences", "accept", "back to top", "privacy choices", "share", "login", "sign in"}
ACTION_LABEL = re.compile(r"^(?:buy|submit|subscribe|unsubscribe|checkout|pay|sign in|log in|register|delete)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    title: str
    url: str


@dataclass(frozen=True)
class Page:
    title: str
    url: str
    text: str = ""
    links: tuple[Link, ...] = ()


def compact(value):
    return " ".join(value.split())


class PageHTML(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.host = urlsplit(url).hostname
        self.ignored = []
        self.in_head = False
        self.in_title = False
        self.primary = 0
        self.title = []
        self.text = []
        self.content = []
        self.link_url = None
        self.link_label = []
        self.links = []
        self.locations = {url}

    def boundary(self):
        self.text.append("\n")
        if self.primary:
            self.content.append("\n")

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if self.ignored:
            if tag not in VOID:
                self.ignored.append(tag)
            return
        if tag in BLOCKED or HIDDEN.intersection(attrs) or attrs.get("aria-hidden") == "true":
            if tag not in VOID:
                self.ignored.append(tag)
            return
        if tag == "head":
            self.in_head = True
        elif tag == "title":
            self.in_title = True
        elif tag in {"main", "article"}:
            self.primary += 1
        elif tag == "a":
            href = attrs.get("href")
            if href and "download" not in attrs and "onclick" not in attrs:
                self.link_url = href
                self.link_label = []
        if tag in BREAKS:
            self.boundary()

    def handle_endtag(self, tag):
        if self.ignored:
            if tag in self.ignored:
                while self.ignored.pop() != tag:
                    pass
            return
        if tag == "title":
            self.in_title = False
        elif tag == "head":
            self.in_head = False
        elif tag == "a":
            self.finish_link()
        if tag in BREAKS:
            self.boundary()
        if tag in {"main", "article"}:
            self.primary = max(0, self.primary - 1)

    def handle_data(self, data):
        if self.ignored:
            return
        if self.in_title:
            self.title.append(data)
        if self.in_head or self.in_title:
            return
        self.text.append(data)
        if self.primary:
            self.content.append(data)
        if self.link_url:
            self.link_label.append(data)

    def finish_link(self):
        href, self.link_url = self.link_url, None
        if not href or len(self.links) >= 10:
            return
        title = compact("".join(self.link_label))[:110]
        if len(title) < 3 or title.casefold() in LINK_SKIP or ACTION_LABEL.search(title):
            return
        try:
            url = public_url(urljoin(self.url, href))
        except WebReadError:
            return
        if url not in self.locations and urlsplit(url).hostname == self.host:
            self.locations.add(url)
            self.links.append(Link(title, url))

    def page(self):
        main = self.lines(self.content)
        body = main if len(main) > 200 else self.lines(self.text)
        return Page(compact("".join(self.title))[:150] or self.host, self.url, body, tuple(self.links))

    @staticmethod
    def lines(values):
        return "\n".join(dict.fromkeys(text for line in "".join(values).splitlines() if (text := compact(line))))[:55_000]


def parse_page(document):
    if document.content_type == "text/plain":
        return Page(urlsplit(document.url).hostname, document.url, document.content.strip()[:55_000])
    parser = PageHTML(document.url)
    parser.feed(document.content)
    parser.close()
    return parser.page()
