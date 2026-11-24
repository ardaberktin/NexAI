import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from nex.accounts.http import AccountError
from nex.feed import Feed
from nex.feed_prompt import plan_topics
from nex.goal_checks import WatchCheck
from nex.goals import Goals
from nex.news_sources import collect, parse_stories
from nex.store import Store
from nex.web_transport import Document
from nex.workspace_content import artifacts, chat_history, ideas
from nex.workspace_features import FeatureDisabled, settings, update
from nex.workspace_http import workspace_request
from nex.workspace_schema import WorkspaceConflict


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), min_free=0, quota=2**30)
        self.anchor = datetime(2026, 11, 18, 16, tzinfo=UTC)
        self.zone = "America/Vancouver"

    def story(self, identifier="a" * 24):
        return {"id": identifier, "title": "A Canadian science story", "url": "https://example.com/news",
                "image": None, "summary": "New research", "topic": "Science", "source": "Publisher",
                "published": self.anchor.isoformat()}

    def test_first_edition_and_next_local_day_are_built_once_and_can_be_saved(self):
        feed = Feed(self.store)
        self.assertTrue(feed.overview(self.zone, anchor=self.anchor)["pending"])
        with patch("nex.feed.collect", return_value=[self.story()]) as collect:
            feed.run(anchor=self.anchor)
            feed.run(anchor=self.anchor + timedelta(hours=1))
            feed.run(anchor=self.anchor + timedelta(hours=20))
            collect.assert_called_once()
            feed.run(anchor=self.anchor + timedelta(days=1))
        edition = feed.overview(self.zone, anchor=self.anchor + timedelta(days=1))
        self.assertEqual(edition["editions"], ["2026-11-19", "2026-11-18"])
        self.assertEqual(edition["stories"], [self.story()])
        feed.save("a" * 24, True)
        self.assertEqual(feed.overview(self.zone, anchor=self.anchor)["saved"], [self.story()])
        self.assertIn("https://example.com/news", artifacts(self.store)[0]["text"])
        feed.save("a" * 24, False)
        self.assertEqual(feed.overview(self.zone, anchor=self.anchor)["saved"], [])

    def test_feed_paginates_beyond_recent_editions_and_skips_days_without_news(self):
        feed = Feed(self.store)
        self.assertIsNone(feed.overview(self.zone, anchor=self.anchor)["previous_day"])
        days = [(self.anchor.date() - timedelta(days=index * 2)).isoformat() for index in range(33)]
        stories = [self.story(f"{index:024x}") for index in range(len(days))]
        with self.store.connect() as db:
            db.executemany("INSERT INTO feed_editions VALUES(?,?,?)", (
                (day, self.anchor.isoformat(), json.dumps([story])) for day, story in zip(days, stories)
            ))

        page = feed.overview(self.zone, anchor=self.anchor)
        self.assertEqual(page["editions"], days[:30])
        for day, story, previous in zip(days, stories, [*days[1:], None]):
            self.assertEqual(page["day"], day)
            self.assertEqual(page["stories"], [story])
            self.assertEqual(page["previous_day"], previous)
            if previous:
                page = feed.overview(self.zone, day=page["previous_day"], anchor=self.anchor)

        feed.save(stories[-1]["id"], True)
        self.assertEqual(feed.overview(self.zone, anchor=self.anchor)["saved"], [stories[-1]])

    def test_feed_drops_stale_work_after_settings_change_and_recovers_from_sources_down(self):
        feed = Feed(self.store)
        original = feed.overview(self.zone, anchor=self.anchor)["settings"]
        original.pop("prompt")
        changed = original | {"topics": ["Science"], "instructions": "Prefer Canadian research."}

        def edited_while_fetching(_, __):
            feed.update(changed, refresh_now=True, anchor=self.anchor)
            return [self.story("b" * 24)]

        with patch("nex.feed.collect", side_effect=edited_while_fetching):
            feed.run(anchor=self.anchor)
        with self.assertRaises(WorkspaceConflict):
            feed.update(original)
        self.assertEqual(feed.overview(self.zone, anchor=self.anchor)["stories"], [])
        with patch("nex.feed.collect", side_effect=[[], [self.story()]]) as collect:
            feed.run(anchor=self.anchor)
            self.assertIsNotNone(feed.overview(self.zone, anchor=self.anchor)["error"])
            feed.run(anchor=self.anchor + timedelta(minutes=1))
            feed.run(lambda *_: "[0]", anchor=self.anchor + timedelta(minutes=16))
            self.assertEqual(collect.call_count, 2)
        result = feed.overview(self.zone, anchor=self.anchor + timedelta(minutes=17))
        self.assertEqual(result["stories"], [self.story()])
        self.assertIsNone(result["error"])
        self.assertEqual(result["settings"]["instructions"], changed["instructions"])
        self.assertEqual(result["settings"]["prompt"], "Science. Prefer Canadian research.")

    def test_feed_switch_discards_inflight_editions_and_keeps_feed_data(self):
        feed = Feed(self.store)
        feed.overview(self.zone, anchor=self.anchor)

        def toggle_while_fetching(_, __):
            update(self.store, "feed", False)
            update(self.store, "feed", True)
            return [self.story()]

        with patch("nex.feed.collect", side_effect=toggle_while_fetching):
            feed.run(anchor=self.anchor)
        self.assertFalse(feed.overview(self.zone, anchor=self.anchor)["stories"])
        with patch("nex.feed.collect", return_value=[self.story()]) as collect:
            feed.run(anchor=self.anchor)
            feed.save("a" * 24, True)
            update(self.store, "feed", False)
            feed.run(anchor=self.anchor + timedelta(days=1))
            collect.assert_called_once()
        with self.assertRaises(FeatureDisabled):
            feed.refresh()
        suggest = Mock(return_value='{"ideas":[]}')
        ideas(self.store, suggest)
        model_context = json.loads(suggest.call_args.args[0][-1]["content"])
        self.assertEqual(model_context["saved_stories"], [])
        self.assertEqual(model_context["feed_interests"], "")
        self.assertEqual(len(artifacts(self.store)), 1)
        reopened = Store(self.store.root, min_free=0, quota=2**30)
        self.assertFalse(settings(reopened)["feed"])
        update(reopened, "feed", True)
        self.assertEqual(feed.overview(self.zone, anchor=self.anchor)["saved"], [self.story()])

    def test_feed_prompt_rebuilds_a_balanced_edition_and_persists_the_schedule(self):
        feed = Feed(self.store)
        original = feed.overview(self.zone, anchor=self.anchor)["settings"]
        example = "Canada news, Turkiye news, global news, f1 news"
        for invalid in ("  ", "a" * 1201):
            with self.assertRaises(ValueError):
                feed.update(original | {"prompt": invalid})
        with patch("nex.feed.collect", return_value=[self.story()]):
            feed.run(anchor=self.anchor)
        payload = {"settings": original | {"prompt": f"  {example}  ", "hour": 9, "minute": 15}, "refresh_now": True}
        api = SimpleNamespace(app=SimpleNamespace(store=self.store), object=lambda _: payload)
        response = workspace_request(api, "PUT", "/v1/workspace/feed", {}, "mac", AssertionError)
        self.assertTrue(response["pending"])
        topics = ["Canada", "Türkiye", "World", "Formula 1"]
        candidates = [self.story(str(index) * 24) | {"topic": topic} for index, topic in enumerate(topics)]

        def curate(messages, _):
            self.assertIn(example, messages[-1]["content"])
            return json.dumps(topics) if messages[-1]["content"] == example else "[0]"

        with patch("nex.feed.collect", return_value=candidates) as collect:
            feed.run(curate, self.anchor)
        collect.assert_called_once_with(topics, self.anchor)
        reopened = Store(self.store.root, min_free=0, quota=2**30)
        edition = Feed(reopened).overview(self.zone, anchor=self.anchor)
        self.assertEqual(edition["settings"]["prompt"], example)
        self.assertEqual((edition["settings"]["hour"], edition["settings"]["minute"]), (9, 15))
        self.assertEqual([story["topic"] for story in edition["stories"]], ["Canada"])

        def suggest(messages, _):
            self.assertEqual(json.loads(messages[1]["content"])["feed_interests"], example)
            return json.dumps({"ideas": [{"title": "Explore a story", "detail": "Compare two sources.",
                                         "prompt": f"Find a story matching {example}", "destination": "chat"}]})

        card, = ideas(reopened, suggest)
        self.assertIn(example, card["prompt"])
        self.assertFalse(card["opens_goal"])

    def test_unchanged_feed_settings_keep_the_edition_and_revision(self):
        feed = Feed(self.store)
        feed.overview(self.zone, anchor=self.anchor)
        with patch("nex.feed.collect", return_value=[self.story()]) as collect:
            feed.run(anchor=self.anchor)
            original = feed.overview(self.zone, anchor=self.anchor)
            settings = original["settings"]
            feed.update(settings | {"prompt": f"  {settings['prompt']}\n"}, refresh_now=True, anchor=self.anchor)
            feed.run(anchor=self.anchor + timedelta(minutes=1))
            collect.assert_called_once()
        self.assertEqual(feed.overview(self.zone, anchor=self.anchor), original)

    def test_deferred_feed_settings_apply_to_the_next_scheduled_edition(self):
        feed = Feed(self.store)
        original = feed.overview(self.zone, anchor=self.anchor)["settings"]
        with patch("nex.feed.collect", return_value=[self.story()]):
            feed.run(anchor=self.anchor - timedelta(days=1))

        # Moving the edition time into the past must not make today's build immediately due.
        saved_at = self.anchor.astimezone(ZoneInfo(self.zone)).replace(hour=6, minute=0)
        feed.update(original | {"prompt": "Science", "hour": 5}, anchor=saved_at)
        reopened = Store(self.store.root, min_free=0, quota=2**30)
        feed = Feed(reopened)
        latest = feed.overview(self.zone, anchor=saved_at)
        self.assertEqual(latest["day"], "2026-11-17")
        self.assertEqual(latest["stories"], [self.story()])
        self.assertEqual(latest["settings"]["prompt"], "Science")
        self.assertFalse(latest["pending"])

        next_edition = saved_at + timedelta(hours=23)
        updated_story = self.story("b" * 24)
        with patch("nex.feed.collect", return_value=[updated_story]) as collect:
            feed.run(anchor=saved_at)
            feed.run(anchor=next_edition - timedelta(minutes=1))
            collect.assert_not_called()
            feed.run(Mock(side_effect=['["Science"]', "[0]"]), anchor=next_edition)
            collect.assert_called_once_with(["Science"], next_edition)
        self.assertEqual(feed.overview(self.zone, anchor=next_edition)["stories"], [updated_story])

    def test_old_feed_row_gets_an_editable_prompt_on_database_upgrade(self):
        feed = Feed(self.store)
        feed.overview(self.zone, anchor=self.anchor)
        with self.store.connect() as db:
            db.execute("UPDATE feed_settings SET instructions='Prioritize climate policy.'")
            db.execute("ALTER TABLE feed_settings DROP COLUMN prompt")
            db.execute("ALTER TABLE feed_settings DROP COLUMN deferred_until_day")
        reopened = Store(self.store.root, min_free=0, quota=2**30)
        settings = Feed(reopened).overview(self.zone, anchor=self.anchor)["settings"]
        self.assertEqual(settings["prompt"], "Canada, World, Technology, Science. Prioritize climate policy.")
        self.assertEqual((settings["hour"], settings["minute"], settings["zone"]), (7, 0, self.zone))

    def test_feed_exclusion_goes_to_the_model_even_when_each_clause_ends_in_news(self):
        generate = Mock(return_value='["Canada", "Politics"]')
        prompt = "Canada news, no celebrity news"
        self.assertEqual(plan_topics(prompt, generate), ["Canada"])
        self.assertEqual(generate.call_args.args[0][-1]["content"], prompt)
        self.assertEqual(plan_topics(prompt, None), [])

    def test_prose_prompt_can_find_a_topic_outside_the_publisher_list(self):
        prompt = "Follow Vancouver housing policy and F1, with an emphasis on race results."

        def plan(messages, _):
            self.assertEqual(messages[-1]["content"], prompt)
            return '["Vancouver housing policy", "Formula One"]'

        topics = plan_topics(prompt, plan)
        self.assertEqual(topics, ["Vancouver housing policy", "Formula 1"])
        document = Document("https://www.bing.com/news/search", """<rss xmlns:news="https://www.bing.com/news/search">
          <channel><item><title>Vancouver votes on housing</title><news:Source>Example News</news:Source>
          <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3A%2F%2Fexample.com%2Fhousing</link>
          <description>City council voted on the proposal.</description>
          <pubDate>Wed, 18 Nov 2026 14:00:00 GMT</pubDate></item></channel></rss>""", "application/rss+xml")
        with patch("nex.news_sources.fetch", return_value=document) as fetch:
            stories = collect(topics[:1], self.anchor)
        self.assertTrue(fetch.call_args.kwargs["search"])
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        self.assertEqual(query["q"], ["Vancouver housing policy"])
        self.assertEqual([(story["url"], story["source"]) for story in stories],
                         [("https://example.com/housing", "Example News")])

    def test_parser_keeps_dated_public_links_and_strips_markup(self):
        body = """<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>
          <item><media:thumbnail url="https://i.guim.co.uk/img/media/photo.jpg?w=140" width="140"/>
          <media:content url="https://i.guim.co.uk/img/media/photo.jpg?w=700&amp;s=example" width="700"/><title>A headline</title><description><![CDATA[<p><em>Latest</em> update</p><p>Second paragraph</p>]]></description>
          <link>https://example.com/articles/update?source=feed</link>
          <pubDate>Wed, 18 Nov 2026 14:00:00 GMT</pubDate></item>
          <item><title>Old</title><link>https://example.com/old</link>
          <pubDate>Wed, 18 Nov 2020 14:00:00 GMT</pubDate></item>
          <item><title>Local</title><link>https://archive.local/news</link>
          <pubDate>Wed, 18 Nov 2026 14:00:00 GMT</pubDate></item>
        </channel></rss>"""
        rows = parse_stories(body, "World", "Publisher", self.anchor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "Latest update")
        self.assertEqual(rows[0]["url"], "https://example.com/articles/update")
        self.assertEqual(rows[0]["image"], "https://i.guim.co.uk/img/media/photo.jpg?w=700&s=example")
        self.assertEqual(parse_stories("<!DOCTYPE rss>" + body, "World", "Publisher", self.anchor), [])

    def test_feed_uses_the_alternate_publisher_when_the_primary_serves_html(self):
        primary = Document("https://www.theguardian.com/world/rss", "<html>Please try again</html>", "text/html")
        alternate = Document("https://feeds.bbci.co.uk/news/world/rss.xml", """<rss><channel>
          <item><title>A headline</title><link>https://www.bbc.com/news/articles/sample</link>
          <pubDate>Wed, 18 Nov 2026 14:00:00 GMT</pubDate></item>
        </channel></rss>""", "text/xml")
        with patch("nex.news_sources.fetch", side_effect=[primary, alternate]) as fetch:
            rows = collect(["World"], self.anchor)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([(row["topic"], row["source"]) for row in rows], [("World", "BBC News")])

    def draft(self, **fields):
        return {"prompt": "Remind me to bring my notes", "zone": self.zone, **fields}

    def test_goals_switch_pauses_scheduled_work_and_blocks_creation(self):
        goals = Goals(self.store, generate=Mock(side_effect=['{"tools":[]}', "Bring your notes"]))
        due = self.anchor + timedelta(minutes=10)
        identifier = str(uuid4())
        draft = self.draft(scheduled_at=due.isoformat())
        goals.save(identifier, draft, anchor=self.anchor)
        update(self.store, "goals", False)
        goals.run(due)
        with self.assertRaises(FeatureDisabled):
            goals.save(str(uuid4()), draft, anchor=self.anchor)
        self.assertIsNotNone(goals.detail(identifier)["next_run"])
        self.assertEqual(artifacts(self.store), [])
        generated = ('{"ideas":[{"title":"Watch for an update","detail":"Check for a follow-up.",'
                     '"prompt":"Check every morning","destination":"goal"}]}')
        self.assertEqual(ideas(self.store, Mock(return_value=generated)), [])
        update(self.store, "goals", True)
        goals.run(due)
        self.assertEqual(artifacts(self.store)[0]["text"], "Bring your notes")

    def test_reminder_fires_once_and_produces_chat_update_and_file(self):
        identifier = str(uuid4())
        due = self.anchor + timedelta(minutes=10)
        goals = Goals(self.store)
        goal = goals.save(identifier, self.draft(scheduled_at=due.isoformat()), anchor=self.anchor)
        goals.run(due - timedelta(seconds=1))
        self.assertEqual(len(goals.detail(identifier)["activity"]), 1)

        # A new Store instance processes the same archive after a service restart.
        reopened = Store(self.store.root, min_free=0, quota=2**30)
        Goals(reopened, generate=Mock(side_effect=['{"tools":[]}', "Bring your notes"])).run(due)
        goals.run(due + timedelta(minutes=2))
        self.assertIsNone(goals.detail(identifier)["next_run"])
        self.assertEqual([item["title"] for item in goals.detail(identifier)["activity"]], [
            "Goal update", "Goal created",
        ])
        self.assertEqual(artifacts(reopened, identifier)[0]["text"], "Bring your notes")
        self.assertEqual(chat_history(reopened)[0]["goal_id"], identifier)
        goals.action(identifier, "completed", goal["revision"], anchor=due)

    def test_watch_recovers_deduplicates_matches_and_ignores_an_inflight_paused_check(self):
        identifier = str(uuid4())
        plan = '{"tools":[{"name":"new_mail"}]}'
        generate = Mock(side_effect=[plan, plan, "Appointment confirmed", plan, plan, "Appointment changed"])
        goals = Goals(self.store, generate=generate)
        goal = goals.save(identifier, self.draft(prompt="Notify me of new appointment emails", frequency_minutes=15,
                                                scheduled_at=self.anchor.isoformat()), anchor=self.anchor)
        first = WatchCheck(("first",), evidence=({"subject": "Appointment confirmed"},))
        with patch("nex.goal_runner.check_watch", side_effect=[AccountError("Reconnect Gmail."), first, WatchCheck()]):
            goals.run(self.anchor)
            self.assertEqual(goals.detail(identifier)["error"], "Reconnect Gmail.")
            goals.run(self.anchor + timedelta(minutes=15))
            goals.run(self.anchor + timedelta(minutes=30))
        self.assertEqual(len(artifacts(self.store, identifier)), 1)
        self.assertIsNone(goals.detail(identifier)["error"])

        def paused_while_waiting(*_):
            goals.action(identifier, "paused", goal["revision"], self.anchor + timedelta(minutes=45))
            return WatchCheck(("second",), evidence=({"subject": "Appointment changed"},))

        with patch("nex.goal_runner.check_watch", side_effect=paused_while_waiting):
            goals.run(self.anchor + timedelta(minutes=45))
        self.assertEqual(len(artifacts(self.store, identifier)), 1)
        self.assertEqual(goals.detail(identifier)["status"], "paused")

    def test_chat_history_restores_typed_threads_without_daybook_questions(self):
        normal, scoped = (str(uuid4()) for _ in range(2))
        with self.store.connect() as db:
            for identifier, scope in ((normal, None), (scoped, "daybook scope")):
                self.store.add_memory(db, "q-" + identifier, self.anchor.isoformat(),
                                      "question", "iphone", "A question", engine=scope)
                self.store.add_memory(db, "a-" + identifier, self.anchor.isoformat(),
                                      "answer", "iphone", "An answer")
        self.assertEqual([message["id"] for message in chat_history(self.store)],
                         ["q-" + normal, "a-" + normal])
