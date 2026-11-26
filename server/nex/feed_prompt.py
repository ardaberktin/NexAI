"""Turn a reader's feed prompt into a bounded set of public news topics."""

import json
import re

from .models import ModelError
from .news_sources import SOURCES
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
        if not key or len(topic) > MAX_TOPIC_LENGTH or not any(char.isalnum() for char in key):
            continue
        topics.setdefault(key, TOPIC_NAMES.get(key, topic))
    return list(dict.fromkeys(topics.values()))[:MAX_TOPICS]


def plan_topics(prompt, generate):
    parts = re.split(r"[,;\n]+", prompt)
    fallback = clean_topics(parts)
    # Preserve each item in short, explicit lists such as "Canada news, F1 news".
    if len(parts) > 1 and all(normalized(part.strip()).endswith(" news") for part in parts):
        return fallback
    if generate:
        try:
            output = generate([
                {"role": "system", "content": "Extract up to eight short topics to search a public news index. "
                 "Include every distinct news interest the reader asks for. Omit exclusions, editorial directions, "
                 "URLs, private details, and personal information. Keep topics specific to the prompt. "
                 "Reply with only a JSON array of topic strings, for example [\"Canada\", \"Formula 1\"]."},
                {"role": "user", "content": prompt},
            ], 160)
            match = re.search(r"\[[^\[\]]*\]", output or "")
            if match and (topics := clean_topics(json.loads(match[0]))):
                return topics
        except (ModelError, ValueError):
            pass
    return fallback
