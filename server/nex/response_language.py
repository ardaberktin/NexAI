import re

from .store import normalized
from .wake import TURKISH_QUESTION


def reply_language(text, preference="auto"):
    if preference in {"en", "tr"}:
        return preference
    if re.search(r"[çÇğĞıİöÖşŞüÜ]", text) or TURKISH_QUESTION.search(normalized(text)):
        return "tr"
    return "en"
