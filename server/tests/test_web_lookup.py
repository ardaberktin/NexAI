import json
import socket
import tempfile
import unittest
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from conversation_fixtures import model
from nex.assistant import Assistant
from nex.store import Store
from nex.web_answers import follow_link, save_pages
from nex.web_html import Link, Page, parse_page
from nex.web_intent import is_web_request, web_request
from nex.web_search import parse_lite
from nex.web_transport import (
    Document,
    WebReadError,
    fetch_public,
    open_response,
    public_url,
    resolve_public,
    routable,
)


class WebBoundaryTests(unittest.TestCase):
    def test_public_search_contains_only_keywords_from_this_request(self):
        request = web_request("Could you search the web for NASA Artemis II?")
        self.assertEqual(request.terms, "NASA Artemis II")
        self.assertFalse(is_web_request("What did I work on yesterday?"))
        self.assertFalse(is_web_request("Look up my Daybook"))
        for question in ("Search the web for my private notes", "Look up person@example.com",
                         "Search web for 604-555-0123", "Look up password hunter2", "Look up 123 Main Street"):
            with self.subTest(question=question), self.assertRaises(WebReadError):
                web_request(question)

    def test_does_not_send_site_queries_or_follow_action_and_private_urls(self):
        request = web_request("Read example.org/guide?token=private#account")
        self.assertEqual(request.url, "https://example.org/guide")
        blocked = (
            "https://user:pass@example.org/",
            "https://example.org:8443/",
            "https://192.168.1.1/",
            "https://host.tailnet.ts.net/",
            "https://example.org/%6cogin",
            "https://example.org/subscriber/unsubscribe/key",
            "https://example.org/%256cogin",
            "https://example.org/abc@example.com",
        )
        for url in blocked:
            with self.subTest(url=url), self.assertRaises(WebReadError):
                public_url(url)

    def test_refuses_mixed_dns_and_special_addresses(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with patch("nex.web_transport.socket.getaddrinfo", return_value=[public, private]), self.assertRaises(WebReadError):
            resolve_public("example.org")
        for address in ("100.64.0.1", "169.254.169.254", "ff02::1", "::ffff:8.8.8.8", "64:ff9b::a00:1"):
            self.assertFalse(routable(address))

    def test_uses_pinned_get_without_user_credentials_or_cookies(self):
        parts = urlsplit("https://example.org/read")
        with (patch("nex.web_transport.resolve_public", return_value=["1.1.1.1"]),
              patch("nex.web_transport.PinnedHTTPS") as connection):
            open_response(parts, monotonic() + 2)
            self.assertEqual(connection.call_args.args[:2], ("example.org", "1.1.1.1"))
            request = connection.return_value.request.call_args
            self.assertEqual(request.args, ("GET", "/read"))
            self.assertTrue(set(request.kwargs["headers"]).isdisjoint({"Cookie", "Referer", "Authorization"}))
            self.assertNotIn("body", request.kwargs)

    def test_private_redirect_is_blocked_before_following(self):
        response = Mock(status=302, headers={"Location": "https://private.local/check"})
        with (patch("nex.web_transport.open_response", return_value=(Mock(), response)) as wire,
              self.assertRaises(WebReadError)):
            fetch_public("https://example.org/read", False, monotonic() + 3)
        self.assertEqual(wire.call_count, 1)


class WebSourcesTests(unittest.TestCase):
    def test_extracts_only_text_and_navigation_that_can_be_read(self):
        body = """
        <html><head><title>Public manual</title></head><body>
        <script>fetch('/track')</script><main><h1>Instructions</h1><p>Useful page text.</p>
        <form action="/write"><p>Secrets in a form</p><input value="private"></form>
        <a href="/details?id=123">More details</a><a href="/danger" onclick="save()">Run script</a>
        <a href="/checkout">Buy this item</a><a href="https://other.org/tracker">External tracker</a>
        </main></body></html>
        """
        page = parse_page(Document("https://example.org/manual", body, "text/html"))
        self.assertEqual(page.title, "Public manual")
        self.assertIn("Useful page text.", page.text)
        self.assertNotIn("Secrets in a form", page.text)
        self.assertNotIn("fetch(", page.text)
        self.assertEqual(page.links, (Link("More details", "https://example.org/details"),))

    def test_unwraps_search_result_without_visiting_tracking_link(self):
        body = ('<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fguide'
                '%3Ftracking%3Dabc&amp;rut=xyz">Guide</a><td class="result-snippet">Useful text</td>')
        self.assertEqual(parse_lite(body), [Page("Guide", "https://example.org/guide", "Useful text")])

    def test_web_answers_bypass_the_personal_context_and_sources_are_locally_clickable(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, 0, 1_048_576)
            engine = Mock()
            engine.generate = model([{"name": "web", "query": "public facts"}], compose="Public answer [1]")
            assistant = Assistant(store, engine, SimpleNamespace(read=dict))
            page = Page("Reference", "https://example.org/guide", "Public answer.", (
                Link("Details", "https://example.org/details"),
            ))
            with (patch("nex.web_answers.lookup", return_value=[page]) as network,
                  patch.object(assistant.personal, "snapshot", side_effect=AssertionError("private context read"))):
                result = assistant.answer("Search web for public facts", "en")
            self.assertEqual(result["text"], "Public answer [1]")
            self.assertEqual(network.call_args.args[0].terms, "public facts")
            self.assertEqual(engine.generate.call_args.args[0][1]["role"], "user")
            self.assertEqual(json.loads(engine.generate.call_args.args[0][1]["content"])["cited_pages"][0]["number"], 1)
            identifier, = result["sources"]
            self.assertEqual(store.source(identifier)["web_title"], "Reference")
            self.assertEqual(store.search(""), [])
            self.assertEqual(save_pages(store, [page]), [identifier])
            with patch("nex.web_answers.read_page", return_value=page) as reader:
                self.assertIsNone(follow_link(store, identifier, 8))
                self.assertIsNotNone(follow_link(store, identifier, 0))
            reader.assert_called_once_with("https://example.org/details")
