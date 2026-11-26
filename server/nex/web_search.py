"""Search public indexes and read their organic result pages."""

import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from .web_html import Page, compact, parse_page
from .web_transport import WebReadError, fetch, public_url

LITE = "https://lite.duckduckgo.com/lite/"
BING = "https://www.bing.com/search"


def destination(value):
    try:
        parts = urlsplit(urljoin(LITE, value))
        if parts.hostname in {"duckduckgo.com", "lite.duckduckgo.com"} and parts.path == "/l/":
            value = parse_qs(parts.query, max_num_fields=8).get("uddg", [""])[0]
        if value.startswith("http://"):
            value = "https://" + value[7:]
        return public_url(value)
    except (ValueError, WebReadError):
        return None


class LiteResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.part = None
        self.text = []
        self.target = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "a" and "result-link" in classes:
            self.target = destination(attributes.get("href") or "")
            self.part, self.text = "a", []
        elif tag == "td" and "result-snippet" in classes:
            self.part, self.text = "td", []

    def handle_data(self, data):
        if self.part:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag != self.part:
            return
        text = compact("".join(self.text))
        if tag == "a" and self.target and text:
            self.items.append(Page(text[:150], self.target))
        elif tag == "td" and self.items and not self.items[-1].text:
            page = self.items[-1]
            self.items[-1] = Page(page.title, page.url, text[:650])
        self.part = None


def parse_lite(body):
    parser = LiteResults()
    parser.feed(body)
    parser.close()
    return parser.items


def parse_feed(body):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", body, re.IGNORECASE):
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    results = []
    for item in root.findall("./channel/item")[:16]:
        url = destination(item.findtext("link", ""))
        title = compact(item.findtext("title", ""))
        if url and title:
            summary = compact(item.findtext("description", ""))
            results.append(Page(title[:150], url, summary[:650]))
    return results


def search(terms):
    for address, parameters, parse in (
        (LITE, {"q": terms}, parse_lite),
        (BING, {"format": "rss", "q": terms}, parse_feed),
    ):
        try:
            body = fetch(address + "?" + urlencode(parameters), search=True, timeout=4).content
            if hits := parse(body):
                return list({page.url: page for page in hits}.values())[:4]
        except WebReadError:
            continue
    raise WebReadError("I couldn’t retrieve public search results. You can give me a specific HTTPS page to read.")


def read_page(url):
    page = parse_page(fetch(url))
    if len(page.text) < 45:
        raise WebReadError("The site didn’t expose enough public text. It may need a browser or a sign-in.")
    return page


def lookup(request):
    if request.url:
        return [read_page(request.url)]
    results = search(request.terms)

    def open_result(result):
        try:
            page = read_page(result.url)
        except WebReadError:
            return result
        # A result can quote the announcement more precisely than the page's navigation text.
        body = page.text
        if result.text and result.text not in body:
            body = result.text + "\n\n" + body
        return Page(result.title or page.title, page.url, body, page.links)

    with ThreadPoolExecutor(max_workers=3) as pool:
        return [*pool.map(open_result, results[:3]), *results[3:]]
