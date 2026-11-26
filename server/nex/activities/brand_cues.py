"""Accept a named platform when the recording contains a current-use or audible interface cue."""

import re

BRANDS = {
    "ps5": r"\b(?:ps\s*(?:5|five)|play\s?station\s*(?:5|five))\b",
    "tiktok": r"\btik[ -]?tok\b", "instagram": r"\binstagram\b", "youtube": r"\byoutube\b",
}
INTERFACE = {
    "ps5": r"\b(?:press the (?:ps|playstation) button|who(?:['’]s| is) using this controller|"
           r"(?:select|choose) (?:a |your )?(?:user|profile) to (?:sign|log) in)\b",
    "tiktok": r"\b(?:(?:for you|following|friends)[,. ]+(?:tab[, ]+)?selected|"
              r"swipe (?:up|down) (?:for|to play) (?:the )?next (?:tik[ -]?tok|video))\b",
    "instagram": r"\b(?:reels?[,. ]+(?:tab[, ]+)?selected|"
                 r"swipe (?:up|down) (?:for|to play) (?:the )?next reel)\b",
}
CURRENT = re.compile(r"\b(?:i(?:['’]m| am)|we(?:['’]re| are)|right now|currently|şu an)\b", re.IGNORECASE)
MENTION = re.compile(r"\b(?:not|never|wish|going to|talking about|thinking about|yesterday|"
                     r"how to|tutorial|follow me|sponsored|advertisement)\b", re.IGNORECASE)
MENU_NAMES = {"ps5": "a PlayStation 5 controller prompt", "tiktok": "TikTok feed navigation",
              "instagram": "Instagram feed navigation"}


def interface_quote(category, text):
    """Keep the app name and audible navigation together in the quoted evidence."""
    interface = INTERFACE.get(category)
    if interface is None:
        return ""
    for brand in re.finditer(BRANDS[category], text, re.IGNORECASE):
        for cue in re.finditer(interface, text, re.IGNORECASE):
            start, end = min(brand.start(), cue.start()), max(brand.end(), cue.end())
            if end - start <= 120 and not MENTION.search(text[max(0, start - 45):end]):
                return text[start:end]
    return ""


def supports_brand(category, basis, quote):
    if not re.search(BRANDS[category], quote, re.IGNORECASE):
        return False
    if basis == "screen":
        return True
    return not MENTION.search(quote) and bool(CURRENT.search(quote) or interface_quote(category, quote))


def menu_and_speech(text):
    """Separate complete device menu announcements from surrounding spoken sentences."""
    for category, interface in INTERFACE.items():
        if not interface_quote(category, text):
            continue
        brand = BRANDS[category]
        announcement = rf"(?:(?:welcome (?:back )?to|this is) )?{brand}"
        navigation = rf"(?:{interface})(?: to continue)?"
        clause = rf"(?:{announcement}|{navigation})"
        menu = re.compile(rf"{clause}(?:[,.!?: ]+(?:and )?{clause})*[.!?]*", re.IGNORECASE)
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        remainder = " ".join(sentence for sentence in sentences if not menu.fullmatch(sentence))
        return MENU_NAMES[category], remainder
    return None, text


def journal_speech(text):
    menu, speech = menu_and_speech(text)
    return f"The recording contained {menu}. {speech}".strip() if menu else speech
