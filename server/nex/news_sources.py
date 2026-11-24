"""Read dated, attributed stories from public publisher feeds."""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

from .web_transport import WebReadError, fetch, public_url

SOURCES = {
    "Canada": ("CBC News", "https://www.cbc.ca/cmlink/rss-canada"),
    "Türkiye": ("Daily Sabah", "https://www.dailysabah.com/rss/turkiye"),
    "World": ("The Guardian", "https://www.theguardian.com/world/rss"),
    "Technology": ("BBC News", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    "Business": ("The Guardian", "https://www.theguardian.com/business/rss"),
    "Science": ("The Guardian", "https://www.theguardian.com/science/rss"),
    "Climate": ("The Guardian", "https://www.theguardian.com/environment/climate-crisis/rss"),
    "Culture": ("The Guardian", "https://www.theguardian.com/culture/rss"),
    "Sports": ("The Guardian", "https://www.theguardian.com/sport/rss"),
    "Formula 1": ("BBC Sport", "https://feeds.bbci.co.uk/sport/formula1/rss.xml"),
}
ALTERNATE_SOURCES = {
    "World": ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    "Science": ("BBC News", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
    "Business": ("BBC News", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    "Climate": ("Grist", "https://grist.org/feed/"),
    "Culture": ("BBC News", "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"),
    "Sports": ("BBC Sport", "https://feeds.bbci.co.uk/sport/rss.xml"),
    "Türkiye": ("TRT Haber", "https://www.trthaber.com/turkiye_articles.rss"),
    "Formula 1": ("The Guardian", "https://www.theguardian.com/sport/formulaone/rss"),
}
NEWS_SEARCH = "https://www.bing.com/news/search"
DEFAULT_TOPICS = ("Canada", "World", "Technology", "Science")
RELATIVE_DAYS = {"today": 0, "yesterday": 1}
PHOTO_HOSTS = {"i.cbc.ca", "ichef.bbci.co.uk", "media.guim.co.uk", "i.guim.co.uk"}
MEDIA = "{http://search.yahoo.com/mrss/}"


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "li"}:
            self.chunks.append("\x00")

    def handle_endtag(self, tag):
        if tag in {"p", "li"}:
            self.chunks.append("\x00")

    def handle_data(self, data):
        self.chunks.append(data)


def plain(value, limit, *, first_paragraph=False):
    parser = TextOnly()
    parser.feed(value[:8000])
    parser.close()
    paragraphs = [" ".join(block.split()) for block in "".join(parser.chunks).split("\x00") if block.strip()]
    if not paragraphs:
        return ""
    text = paragraphs[0] if first_paragraph else " ".join(paragraphs)
    if len(text) <= limit:
        return text
    return (text[:limit].rpartition(" ")[0] or text[:limit - 1]) + "…"


def https(value):
    try:
        return public_url(value.replace("http://", "https://", 1) if value.startswith("http://") else value)
    except WebReadError:
        return None


def photo(item):
    choices = []
    for element in item.iter():
        if element.tag not in {MEDIA + "content", MEDIA + "thumbnail", "enclosure"}:
            continue
        kind = element.get("type", "")
        if kind and not kind.startswith("image/"):
            continue
        raw = element.get("url", "")
        url = https(raw)
        if url and urlsplit(url).hostname in PHOTO_HOSTS:
            # Approved publisher CDNs may sign the image's query parameters.
            query = urlsplit(raw).query
            width = element.get("width", "")
            size = int(width) if re.fullmatch(r"[0-9]{1,5}", width) else 0
            choices.append((size, url + ("?" + query if query else "")))
    return max(choices, key=lambda choice: choice[0])[1] if choices else None


def indexed_story(item, link):
    try:
        parts = urlsplit(link)
        if parts.hostname in {"www.bing.com", "bing.com"} and parts.path == "/news/apiclick.aspx":
            link = parse_qs(parts.query, max_num_fields=16).get("url", [""])[0]
        url = https(link)
    except ValueError:
        return None, ""
    provider = next((child.text for child in item if child.tag.rsplit("}", 1)[-1].lower() == "source"), "")
    return url, plain(provider or "", 80) or (urlsplit(url).hostname if url else "")


def parse_stories(body, topic, source, anchor):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", body, re.IGNORECASE):
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    rows = []
    for item in root.findall("./channel/item")[:28]:
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            continue
        if not anchor - timedelta(days=7) <= published <= anchor + timedelta(minutes=15):
            continue
        link = item.findtext("link", "").strip()
        url, publisher = (https(link), source) if source else indexed_story(item, link)
        title = plain(item.findtext("title", ""), 220)
        if title and url:
            rows.append({"id": hashlib.sha256(url.encode()).hexdigest()[:24], "title": title,
                         "summary": plain(item.findtext("description", ""), 300, first_paragraph=True),
                         "url": url, "image": photo(item), "topic": topic, "source": publisher,
                         "published": published.isoformat(timespec="milliseconds")})
    return rows


def collect(topics, anchor):
    def read(topic):
        if topic not in SOURCES:
            return search_topic(topic, anchor)
        providers = [SOURCES[topic]]
        if alternate := ALTERNATE_SOURCES.get(topic):
            providers.append(alternate)
        for source, url in providers:
            try:
                document = fetch(url, feed=True, timeout=8)
                if rows := parse_stories(document.content, topic, source, anchor):
                    return rows
            except WebReadError:
                continue
        return search_topic(topic, anchor)

    with ThreadPoolExecutor(max_workers=3) as pool:
        # Keep publisher order when the same article appears under multiple topics.
        stories = {}
        for batch in pool.map(read, topics):
            for row in batch:
                stories.setdefault(row["id"], row)
        return list(stories.values())


def search_topic(topic, anchor):
    url = NEWS_SEARCH + "?" + urlencode({"q": topic, "format": "rss", "mkt": "en-CA"})
    try:
        document = fetch(url, search=True, timeout=8)
    except WebReadError:
        return []
    return parse_stories(document.content, topic, None, anchor)


def validate_news_date(value):
    if value not in RELATIVE_DAYS:
        date.fromisoformat(value)


def news_date(value, local_day):
    if value in RELATIVE_DAYS:
        return local_day - timedelta(days=RELATIVE_DAYS[value])
    return date.fromisoformat(value)


def daily_stories(day, zone, anchor, topic=""):
    """Return dated public headlines from the local day the caller requested."""
    if day > anchor.astimezone(zone).date():
        return []
    if topic:
        category = next((name for name in SOURCES if name.casefold() == topic.casefold()), topic)
        topics = (category,)
    else:
        topics = DEFAULT_TOPICS
    rows = collect(topics, anchor)
    stories = [row for row in rows if datetime.fromisoformat(row["published"]).astimezone(zone).date() == day]
    if topic:
        return sorted(stories, key=lambda row: row["published"], reverse=True)[:5]
    return [newest for name in topics
            if (newest := max((row for row in stories if row["topic"] == name),
                              key=lambda row: row["published"], default=None))]
