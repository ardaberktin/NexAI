"""Ground mixed-input classifications in a selected, captured observation."""

import json

from ..model_context import constrained_messages
from ..personal_files import safe_line
from .brand_cues import BRANDS, identifies_brand
from .recognition import (
    CLASSIFIER_CATEGORIES,
    SOUND_CATEGORIES,
    TITLES,
    Observation,
    heard,
    numbered_sounds,
    read_decision,
)

SCREEN_STATES = ("active", "idle", "unclear")
INSTRUCTION = """Classify what is happening now. This recording includes a shared screen observation and possibly room audio.
First consider whether the screen shows active use or idle content. Active playback or an ongoing game is direct evidence;
a paused or idle player is not. The audio may come from the active playback. A narration about doing an activity is not
evidence the viewer is doing it. If the screen is idle, use the distinctive sound activity if one exists.
When a platform is clearly in active use, choose the named category. YouTube videos and YouTube Shorts both use youtube.
Use livestream when a live stream or live broadcast is actively playing, tv for television, ps5 for PlayStation 5,
gaming for other games, media for generic video playback, show for an episode recap, or other with a concise title
(2-4 words) when a clear visible activity does not fit the named categories.
Pick the supporting evidence key. Return a JSON object. Available categories: """ + ", ".join(
    f"{key} ({TITLES[key]})" for key in CLASSIFIER_CATEGORIES
)


def screen_messages(evidence):
    properties = {
        "screen_state": {"enum": list(SCREEN_STATES)},
        "evidence": {"enum": ["screen", *(key for key in evidence if key != "screen"), "none"]},
        "category": {"enum": list(CLASSIFIER_CATEGORIES)},
        "title": {"type": "string", "maxLength": 60},
    }
    messages = [{"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]
    return constrained_messages(messages, {"type": "object", "properties": properties,
                                           "required": ["screen_state", "evidence", "category"],
                                           "additionalProperties": False})


def resolve_screen_decision(value, evidence, fallback):
    """Accept a cited observation only when its source is available and usable."""
    if value is None or value.get("screen_state") not in SCREEN_STATES:
        return None
    category, key = value["category"], value.get("evidence")
    if category == "unknown" or not isinstance(key, str) or key not in evidence:
        return fallback
    title = safe_line(value.get("title"), 60) if category == "other" else None
    if category == "other" and (not title or len(title) < 3):
        return fallback
    basis = key if key in ("screen", "speech") else "sound"
    if basis == "screen" and value["screen_state"] != "active":
        return fallback
    if basis == "sound" and category not in SOUND_CATEGORIES:
        return fallback
    detail = safe_line(evidence[key], 240)
    if not detail or (category in BRANDS and not identifies_brand(category, detail)):
        return fallback
    return Observation(category, basis, heard([detail]) if basis == "sound" else detail, title=title)


async def classify_with_screen(dialogue, transcript, screen, sounds, fallback):
    evidence = {f"sound{key}": label for key, label in numbered_sounds(sounds).items()}
    if transcript:
        evidence["speech"] = transcript
    evidence["screen"] = screen
    raw = await dialogue.background_text(screen_messages(evidence), 110, temperature=0)
    return resolve_screen_decision(read_decision(raw), evidence, fallback)
