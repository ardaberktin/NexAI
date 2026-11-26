import json
import unittest
from unittest.mock import AsyncMock, Mock

from nex.activities.recognition import Observation, from_sounds
from nex.activities.voice_origin import review_voice_origin, source_evidence


class PlaybackReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_pause_excludes_the_earlier_playback_cue(self):
        transcript = "Pause the video, please. Okay, it's paused. What did you want to ask?"
        sounds = ["Speech", "Conversation"]
        evidence = source_evidence(transcript, sounds, [{"seconds_before": 3, "labels": ["Television"]}])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            '{"stopped":true}', '{"origin":"room_conversation","evidence":"words"}',
        ]))

        result = await review_voice_origin(dialogue, evidence, from_sounds(sounds, "ambient", transcript))

        classification = dialogue.background_text.await_args.args[0]
        self.assertEqual(json.loads(classification[-1]["content"])["earlier_playback"], [])
        self.assertEqual(result, Observation("conversation", "speech", transcript))

    async def test_model_can_connect_current_dialogue_with_earlier_playback(self):
        transcript = "Didn't that happen last week? I thought it was later."
        sounds = ["Speech", "Conversation"]
        evidence = source_evidence(transcript, sounds, [{"seconds_before": 3, "labels": ["Television"]}])
        dialogue = Mock(background_text=AsyncMock(side_effect=[
            '{"stopped":false}', '{"continuity":"continues"}',
        ]))

        result = await review_voice_origin(dialogue, evidence, from_sounds(sounds, "ambient", transcript))

        self.assertEqual(result, Observation("media", "sound", "Heard Speech, Conversation. Nearby recordings picked up Television."))
