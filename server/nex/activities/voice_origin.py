"""Resolve ambiguous voices using the model and captured playback evidence."""

import json

from ..live import MEDIA_CUE_LABELS, PLAYBACK_LABELS
from ..model_context import constrained_messages
from ..personal_files import safe_line
from .playback_continuity import playback_continuity
from .recognition import Observation, device_playback_detail, heard, read_object

ORIGINS = {"uncertain_speech": "speech", "recorded_playback": "media", "room_conversation": "conversation"}

INSTRUCTION = """Identify where the current audible voices came from. Use captured facts, not guesses.
The measured playback fields summarize acoustic model detections of devices such as radio or television.
If device_playback reports that media audio is actively playing on the device, choose recorded_playback unless words
clearly show a live call or people directly conversing in the room.
If playback was detected in the current clip and explains the voices, choose recorded_playback.
If playback was detected a few seconds earlier and voices continue, choose recorded_playback unless the current
evidence shows a new activity or that playback stopped.
media_cues lists media-correlated room sounds (such as narration, background music, game music, applause, or laughter).
audio_profile summarizes speaker diarization and whether the enrolled owner's voice was heard. When owner_speaking is
false and speech is continuous or single-speaker, prefer recorded_playback (if playback/media/host cues exist) or
uncertain_speech rather than room_conversation.
A video host, streamer addressing chat or viewers, or broadcast commentary indicates recorded_playback even when reading
questions aloud and answering them.
Conversation from the room requires clear evidence in words of people directly exchanging turns in the current clip.
An acoustic "Conversation" tag alone never establishes room_conversation.
Use uncertain_speech for ordinary or ambiguous speech with no established source, and no_voices when there is no voice cue.
Cite the evidence supporting the decision. Current recorded words and current measured sounds can resolve earlier clues.
For recorded playback using a measurement, cite device_playback, current_playback, earlier_playback, or media_cues.
Do not cite a field that has no measured cue. Return the origin and one evidence field."""

EXAMPLES = (
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [], "words": "The notebook is over there."},
     "uncertain_speech", "current_sounds"),
    ({"current_sounds": ["Speech", "Conversation", "Television"], "current_playback": ["Television"], "earlier_playback": []},
     "recorded_playback", "current_playback"),
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [{"seconds_before": 4, "labels": ["Radio"]}]},
     "recorded_playback", "earlier_playback"),
    ({"current_sounds": ["Speech", "Conversation"], "current_playback": [], "earlier_playback": [],
      "device_playback": {"media_playing": True, "app": "Google Chrome"},
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


def source_evidence(transcript, sounds, nearby, *, audio_context=None, device_context=None):
    earlier = []
    for clip in nearby:
        labels = [label for label in clip["labels"] if label in PLAYBACK_LABELS]
        if labels:
            earlier.append({"seconds_before": clip["seconds_before"], "labels": labels})
    current = sounds or []
    evidence = {"current_sounds": current, "current_playback": [label for label in current if label in PLAYBACK_LABELS],
                "earlier_playback": earlier}
    media_cues = [label for label in current if label in MEDIA_CUE_LABELS]
    if media_cues:
        evidence["media_cues"] = media_cues
    if audio_context:
        evidence["audio_profile"] = audio_context
    if device_context and device_context.get("media_playing"):
        evidence["device_playback"] = device_context
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
    if reference == "words":
        detail = safe_line(selected, 240)
        return Observation(category, "speech", detail) if detail else None
    if reference == "earlier_playback":
        if not (evidence["current_sounds"] or evidence.get("words")):
            return fallback
        return earlier_playback_observation(evidence) if category == "media" else None
    if reference == "media_cues":
        return Observation(category, "sound", heard(selected[:3])) if category == "media" else None
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
