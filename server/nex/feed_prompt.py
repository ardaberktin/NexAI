"""Turn a reader's feed prompt into a bounded set of public news topics."""

import json
import re

from .models import ModelError
from .news_sources import SOURCES
from .personal_files import SECRET
from .store import normalized

MAX_TOPICS = 8
MAX_TOPIC_LENGTH = 100
TOPIC_NAMES = {normalized(topic): topic for topic in SOURCES} | {
    "canadian": "Canada",
    "global": "World",
    "international": "World",
    "turkey": "Türkiye",
    "turkish": "Türkiye",
    "f1": "Formula 1",
    "formula one": "Formula 1",
}


def clean_topics(values):
    topics = {}
    for value in values:
        if not isinstance(value, str):
            continue
        topic = " ".join(value.split()).strip(" .!?")
        key = normalized(topic).removesuffix(" news")
        if (not key or len(topic) > MAX_TOPIC_LENGTH or not any(char.isalnum() for char in key)
                or SECRET.search(topic) or re.search(r"[@=]|https?://", topic, re.IGNORECASE)):
            continue
        topics.setdefault(key, TOPIC_NAMES.get(key, topic))
    return list(dict.fromkeys(topics.values()))[:MAX_TOPICS]


def grounded_topics(values, prompt):
    text = normalized(prompt)
    selected = []
    for topic in clean_topics(values):
        names = {normalized(topic)} | {alias for alias, canonical in TOPIC_NAMES.items() if canonical == topic}
        if any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) for name in names):
            selected.append(topic)
    return selected


def plan_topics(prompt, generate):
    if generate:
        try:
            output = generate([
                {"role": "system", "content": "Extract up to eight short topics to search a public news index. "
                 "Include each distinct news interest in the request. Preserve geographic scope. Do not add topics "
                 "that the reader did not ask to include. Omit exclusions, editorial directions, "
                 "URLs, private details, and personal information. Keep the user's wording for topics, and use [] "
                 "when the prompt contains no affirmative topic. "
                 "Reply with only a JSON array of topic strings, for example [\"Canada\", \"Formula 1\"]."},
                {"role": "user", "content": prompt},
            ], 160)
            match = re.search(r"\[[^\[\]]*\]", output or "")
            if match and (topics := grounded_topics(json.loads(match[0]), prompt)):
                return topics
        except (ModelError, ValueError):
            pass
    return []
