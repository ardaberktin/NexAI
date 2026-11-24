import unittest

from nex.conversation_intent import greeting_reply, wants_archive
from nex.recall_dates import recall_dates


class LiveIntentTests(unittest.TestCase):
    def test_greetings_have_a_short_reply_in_the_spoken_language(self):
        for text, expected in (
            ("Hello", ("Hey!", "en")),
            ("No, I said hello.", ("Hey!", "en")),
            ("Hello, how are you?", ("I’m good. How are you?", "en")),
            ("Merhaba!", ("Merhaba!", "tr")),
            ("Selam, nasılsın?", ("İyiyim, sen nasılsın?", "tr")),
            ("Hello, what did we agree on yesterday?", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(greeting_reply(text, "auto"), expected)

    def test_archive_is_reserved_for_recall_turns(self):
        for text in ("I", "I said hello", "Hello, how", "How are you today?", "Tell me about yourself",
                     "What is an audio recording?", "Can you repeat what you just said?"):
            with self.subTest(text=text):
                self.assertFalse(wants_archive(text, recall_dates(text)))
        for text in ("Hello, what did we agree on yesterday?", "Search my recordings for Orion",
                     "What happened last week?", "2026-11-17", "Dün ne konuştuk?", "Arşivde bul"):
            with self.subTest(text=text):
                self.assertTrue(wants_archive(text, recall_dates(text)))
