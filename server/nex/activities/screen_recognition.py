"""Ground mixed-input classifications in a selected, captured observation."""

import json

from ..model_context import constrained_messages
from ..personal_files import safe_line
from .brand_cues import BRANDS, supports_brand
from .recognition import (
    CLASSIFIER_CATEGORIES,
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
For an active console game use ps5 only with PlayStation 5 evidence. For generic TV playback use media, and use show
for an episode recap. Pick the supporting evidence key. Return a JSON object. Available categories: """ + ", ".join(
    f"{key} ({TITLES[key]})" for key in CLASSIFIER_CATEGORIES
)


def screen_messages(evidence):
    properties = {
        "screen_state": {"enum": list(SCREEN_STATES)},
        "evidence": {"enum": ["screen", *(key for key in evidence if key != "screen"), "none"]},
        "category": {"enum": list(CLASSIFIER_CATEGORIES)},
    }
    messages = [{"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}]
    return constrained_messages(messages, {"type": "object", "properties": properties,
                                           "required": list(properties), "additionalProperties": False})


def resolve_screen_decision(value, evidence, fallback):
    """Return the observation and whether an idle screen should be left out on a second pass."""
    if value is None or value.get("screen_state") not in SCREEN_STATES:
        return None, False
    category, key = value["category"], value.get("evidence")
    if value["screen_state"] == "idle" and (key == "screen" or category == "unknown"):
        return fallback, True
    if category == "unknown" or not isinstance(key, str) or key not in evidence:
        return fallback, False
    basis = key if key in ("screen", "speech") else "sound"
    if basis == "screen" and value["screen_state"] != "active":
        return fallback, False
    detail = safe_line(evidence[key], 240)
    if not detail or (category in BRANDS and (basis == "sound" or not supports_brand(category, basis, detail))):
        return fallback, False
    return Observation(category, basis, heard([detail]) if basis == "sound" else detail), False


async def classify_with_screen(dialogue, transcript, screen, sounds, fallback):
    evidence = {f"sound{key}": label for key, label in numbered_sounds(sounds).items()}
    if transcript:
        evidence["speech"] = transcript
    evidence["screen"] = screen
    raw = await dialogue.background_text(screen_messages(evidence), 110, temperature=0)
    return resolve_screen_decision(read_decision(raw), evidence, fallback)
