"""Translate an existing, cited recap without re-answering the question from archive metadata."""

import re

from .response_language import reply_language
from .store import normalized, search_prefixes


def translation_messages(result, language):
    names = {"en": "English", "tr": "Turkish"}
    return [{"role": "system", "content": "You are a bilingual English and Turkish translator. Return only the "
             "translated passage. Preserve the subject, action, tense, objects, and proper names."},
            {"role": "user", "content": f"Source language: {names[result['language']]}\n"
             f"Target language: {names[language]}\nPassage: {result['text']}"}]


def translation_candidate(result, language, text):
    if not text or reply_language(text) != language:
        return None
    numbers = r"\b\d+\b"
    if set(re.findall(numbers, text)) != set(re.findall(numbers, result["text"])):
        return None
    return result | {"text": text.strip(), "language": language}


def grounded_translation(source, candidate, reverse):
    # Compare independently back-translated terms. A failed check returns the cited original.
    terms = set(search_prefixes(source["text"])) - {"to", "at", "on", "in", "of", "as"}
    recovered = normalized(reverse or "")
    # In the source language this also handles localized names, months and weekdays.
    names = re.findall(r"(?<=[a-zçğıöşü][ ,])([A-ZÇĞİÖŞÜ][^\W\d_]+)\b", source["text"])
    if (any(normalized(name) not in recovered for name in names) or not terms
            or sum(term in recovered for term in terms) / len(terms) < 0.8):
        return source
    return candidate


def translate_recap(source, language, generate):
    text = generate(translation_messages(source, language), 240)
    if candidate := translation_candidate(source, language, text):
        reverse = generate(translation_messages(candidate, source["language"]), 240)
        return grounded_translation(source, candidate, reverse)
    return source
