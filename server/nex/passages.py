import re
from collections import Counter, deque

from .store import excerpt, normalized, search_prefixes

TOKEN = re.compile(r"[^\W_]+")


def matching_passage(text, query, limit=460):
    if len(text) <= limit:
        return text
    terms = search_prefixes(query)
    if not terms:
        return excerpt(text, limit)
    window = deque()
    counts = Counter()
    best = ()
    best_rank = (0, 0)
    # Leave space for the surrounding words and for an ellipsis at either edge.
    for token in TOKEN.finditer(text):
        word = normalized(token.group())
        matches = [term for term in terms if word.startswith(term)]
        if not matches:
            continue
        window.append((token.start(), token.end(), matches))
        counts.update(matches)
        while len(window) > 1 and token.end() - window[0][0] > limit - 90:
            _, _, expired = window.popleft()
            for term in expired:
                counts[term] -= 1
                if counts[term] == 0:
                    del counts[term]
        rank = (len(counts), len(window))
        if rank > best_rank:
            best, best_rank = (window[0][0], token.end()), rank
    if not best:
        return excerpt(text, limit)
    first, last = best
    width = limit - 2
    start = min(max(0, first - max(24, (width - (last - first)) // 2)), len(text) - width)
    end = start + width
    # Avoid partial words when there is a boundary outside the actual match.
    if start and not text[start - 1].isspace():
        start += next((i for i, char in enumerate(text[start:first]) if char.isspace()), 0)
    if end < len(text) and not text[end].isspace():
        space = text.rfind(" ", last, end)
        if space != -1:
            end = space
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
