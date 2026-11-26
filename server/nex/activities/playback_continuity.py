"""Let the model check whether a measured playback source persisted across clips."""

import json

from ..model_context import constrained_messages
from .recognition import read_object

STOP_INSTRUCTION = (
    "Does the quoted transcript explicitly say that a player, video, radio, or TV was paused, stopped, or turned off?\n"
    "Assess only evidence that playback stopped. Ordinary dialogue does not confirm this. Return one JSON field stopped set to\n"
    "true only when the words actually confirm the stop or pause."
)
STOP_EXAMPLES = (
    ({"words": "Could you turn the TV off? Okay, it's off. Where did you put the book?"}, True),
    ({"words": "Did you meet them at the station? They said to arrive early."}, False),
    ({"words": "Can you pause the video? Hang on a second; I want to watch this."}, False),
    ({"words": "The radio's finally off. Can you hear me now? Yes, much better."}, True),
)

CONTINUITY_INSTRUCTION = """Did the previously detected playback likely continue into the clip now being classified?
Use the gap and current sounds. Treat a short uninterrupted stretch of audible speech after a measured radio or TV cue
as a continuation unless the current clip shows a change. Dialogue in a video can contain questions and answers.
A question and a statement cannot by themselves show that playback stopped.
Choose changed if there is affirmative evidence in the current clip of stopping or switching to a different source.
Choose uncertain if the gap is large or the current sound does not link the clips. Return the continuity assessment."""
CONTINUITY_EXAMPLES = (
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [{"seconds_before": 4, "labels": ["Radio"]}]},
     "continues"),
    ({"current_sounds": ["Speech", "Conversation", "Music"], "current_playback": [],
      "earlier_playback": [{"seconds_before": 4, "labels": ["Television"]}],
      "words": "Are they taking the train? They should be at the station by now."}, "continues"),
    ({"current_sounds": ["Speech", "Conversation"], "current_playback": [],
      "earlier_playback": [{"seconds_before": 6, "labels": ["Radio"]}],
      "words": "Turn off the radio. Done. Ready to leave? Yes."}, "changed"),
    ({"current_sounds": ["Speech"], "current_playback": [], "earlier_playback": [{"seconds_before": 80, "labels": ["Television"]}],
      "words": "I'm almost ready."}, "uncertain"),
)


async def assess(dialogue, evidence, instruction, examples, field, schema):
    messages = [{"role": "system", "content": instruction}]
    for sample, outcome in examples:
        messages.extend(({"role": "user", "content": json.dumps(sample)},
                         {"role": "assistant", "content": json.dumps({field: outcome})}))
    messages.append({"role": "user", "content": json.dumps(evidence, ensure_ascii=False)})
    prompt = constrained_messages(messages, {"type": "object", "properties": {field: schema},
                                             "required": [field], "additionalProperties": False})
    reply = read_object(await dialogue.background_text(prompt, 32, temperature=0))
    return reply.get(field) if reply else None


async def playback_continuity(dialogue, evidence):
    stopped = await assess(dialogue, {"words": evidence["words"]}, STOP_INSTRUCTION, STOP_EXAMPLES,
                           "stopped", {"type": "boolean"})
    if stopped is True:
        return "changed"
    if stopped is not False:
        return None
    return await assess(dialogue, evidence, CONTINUITY_INSTRUCTION, CONTINUITY_EXAMPLES,
                        "continuity", {"enum": ["uncertain", "continues", "changed"]})
