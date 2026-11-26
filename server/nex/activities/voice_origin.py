"""Resolve ambiguous voices using the model and captured playback evidence."""

import json

from ..live import PLAYBACK_LABELS
from ..model_context import constrained_messages
from ..personal_files import safe_line
from .playback_continuity import playback_continuity
from .recognition import Observation, device_playback_detail, heard, read_object

ORIGINS = {"uncertain_speech": "speech", "recorded_playback": "media", "room_conversation": "conversation"}

INSTRUCTION = """Identify where the current audible voices came from. Use captured facts, not guesses.
Weigh all available evidence together:
- current_sounds lists all acoustic detections from the room microphone.
- current_playback and earlier_playback summarize TV/radio detections in the current or preceding clips.
- recent_activities lists activities and sounds observed in immediately preceding clips on the same microphone
  (such as watching TV, playing PS5/games, movies, shows, YouTube, or livestreams). Dialogue scenes in ongoing
  media, TV, or game cutscenes are recorded_playback unless evidence shows it stopped or someone in the room started talking.
- device_playback reports active media audio playing on a device. Compare its device with recording_device when present
  before assuming the microphone is hearing that device rather than TV, a console, or the room.
- audio_profile summarizes speaker diarization and whether the enrolled owner's voice was heard. When owner_speaking is
  false and speech is continuous or single-speaker, prefer recorded_playback (if playback, media, host, or recent media
  evidence exists) or uncertain_speech rather than room_conversation.
- A video host, streamer addressing chat/viewers, game dialogue, or TV/broadcast speech is recorded_playback even when
  questions and answers appear in the transcript.
- Conversation from the room requires clear evidence in words of people directly exchanging turns in the current clip.
  An acoustic "Conversation" tag alone never establishes room_conversation.
- Use uncertain_speech for ordinary or ambiguous speech with no established source, and no_voices when there is no voice cue.
Cite the evidence field supporting the decision. Do not cite a field that has no measured cue.
Return the origin and one evidence field."""

EXAMPLES = (
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [], "words": "The notebook is over there."},
     "uncertain_speech", "current_sounds"),
    ({"current_sounds": ["Speech", "Conversation", "Television"], "current_playback": ["Television"], "earlier_playback": []},
     "recorded_playback", "current_playback"),
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [{"seconds_before": 4, "labels": ["Radio"]}]},
     "recorded_playback", "earlier_playback"),
    ({"current_sounds": ["Speech", "Conversation"], "current_playback": [], "earlier_playback": [],
      "recording_device": "mac", "device_playback": {"media_playing": True, "app": "Google Chrome", "device": "mac"},
      "words": "Wait did you see that play? Yeah that was wild."}, "recorded_playback", "device_playback"),
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [],
      "words": "In today's video we'll look at five quick ways to reuse old containers. Subscribe for the next episode."},
     "recorded_playback", "words"),
    ({"current_sounds": ["Speech", "Conversation"], "current_playback": [], "earlier_playback": [],
      "audio_profile": {"speaker_count": 1, "speaker_turns": 0, "speech_ratio": 0.88, "owner_speaking": False},
      "words": "Chat someone just asked why I picked that build, and honestly it feels faster on this patch."},
     "recorded_playback", "words"),
    ({"current_sounds": ["Speech", "Conversation"], "current_playback": [],
      "earlier_playback": [{"seconds_before": 6, "labels": ["Radio"]}],
      "words": "Turn off the radio. Done. Ready to leave? Yes."}, "room_conversation", "words"),
)


def source_evidence(transcript, sounds, nearby, *, recording_device=None,
                    audio_context=None, device_context=None, recent=None):
    earlier = []
    for clip in nearby:
        labels = [label for label in clip["labels"] if label in PLAYBACK_LABELS]
        if labels:
            earlier.append({"seconds_before": clip["seconds_before"], "labels": labels})
    current = sounds or []
    evidence = {"current_sounds": current, "current_playback": [label for label in current if label in PLAYBACK_LABELS],
                "earlier_playback": earlier}
    if recording_device and device_context and device_context.get("media_playing"):
        evidence["recording_device"] = recording_device
    if audio_context:
        evidence["audio_profile"] = audio_context
    if device_context and device_context.get("media_playing"):
        evidence["device_playback"] = device_context
    if recent:
        evidence["recent_activities"] = recent
    if transcript:
        evidence["words"] = transcript
    return evidence


def source_messages(evidence):
    messages = [{"role": "system", "content": INSTRUCTION}]
    for sample, origin, reference in EXAMPLES:
        messages.extend(({"role": "user", "content": json.dumps(sample)},
                         {"role": "assistant", "content": json.dumps({"origin": origin, "evidence": reference})}))
    messages.append({"role": "user", "content": json.dumps(evidence, ensure_ascii=False)})
    properties = {
        "origin": {"enum": [*ORIGINS, "no_voices"]},
        "evidence": {"enum": [*(key for key, value in evidence.items() if value), "none"]},
    }
    return constrained_messages(messages, {"type": "object", "properties": properties,
                                           "required": list(properties), "additionalProperties": False})


def source_observation(raw, evidence, fallback):
    result = read_object(raw)
    if result is None:
        return None
    origin, reference = result.get("origin"), result.get("evidence")
    if origin == "no_voices":
        return fallback
    if not isinstance(origin, str) or origin not in ORIGINS or not isinstance(reference, str):
        return None
    selected = evidence.get(reference)
    if not selected:
        return None
    category = ORIGINS[origin]
    if category == "conversation" and reference != "words":
        return fallback
    if category == "media" and reference == "current_sounds":
        return None
    if reference == "device_playback":
        if category == "media":
            return Observation("media", "device", device_playback_detail(selected))
        return None
    if reference == "audio_profile":
        if category == "speech":
            detail = heard(evidence["current_sounds"][:3]) if evidence["current_sounds"] else "Speech was captured."
            return Observation("speech", "sound", detail)
        return None
    if reference == "recent_activities":
        if category != "media":
            return None
        latest = selected[0] if isinstance(selected, list) and selected else None
        if not isinstance(latest, dict):
            return None
        prior_category = latest.get("category")
        prior_title = latest.get("title")
        current = heard(evidence["current_sounds"][:3]) if evidence["current_sounds"] else "Speech was captured."
        if prior_category and prior_category not in ("unknown", "speech", "conversation", "nex"):
            detail = f"{current} Continued from recent {prior_title.lower()}." if prior_title else current
            custom = prior_title if prior_category == "other" else None
            return Observation(prior_category, "sound", detail, title=custom)
        return Observation("media", "sound", current)
    if reference == "words":
        detail = safe_line(selected, 240)
        return Observation(category, "speech", detail) if detail else None
    if reference == "earlier_playback":
        if not (evidence["current_sounds"] or evidence.get("words")):
            return fallback
        return earlier_playback_observation(evidence) if category == "media" else None
    return Observation(category, "sound", heard(selected[:3]))


def earlier_playback_observation(evidence):
    labels = dict.fromkeys(label for clip in evidence["earlier_playback"] for label in clip["labels"])
    current = heard(evidence["current_sounds"][:3]) if evidence["current_sounds"] else "Speech was captured."
    detail = current + " Nearby recordings picked up " + ", ".join(labels) + "."
    return Observation("media", "sound", detail)


async def review_voice_origin(dialogue, evidence, fallback):
    if (evidence["earlier_playback"] and not evidence["current_playback"]
            and not evidence.get("device_playback") and evidence.get("words")):
        continuity = await playback_continuity(dialogue, evidence)
        if continuity == "continues":
            return earlier_playback_observation(evidence)
        if continuity in ("changed", "uncertain"):
            evidence = evidence | {"earlier_playback": []}
    raw = await dialogue.background_text(source_messages(evidence), 72, temperature=0)
    return source_observation(raw, evidence, fallback)
