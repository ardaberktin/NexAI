import base64
import io
import json
import unittest
import wave
from unittest.mock import Mock

from nex.http_api import API
from nex.live import OUTPUT_RATE
from nex.voice_chime import CHIME_PCM


class VoiceChimeTests(unittest.TestCase):
    def test_recorded_stream_carries_the_live_chime_in_a_playable_wav(self):
        handler = Mock(wfile=io.BytesIO())
        handler.app.worker.voice_streams.events.return_value = [
            {"kind": "chime"}, {"kind": "speech", "text": "Five.", "language": "en"},
        ]
        handler.app.engines.synthesize.return_value = b"spoken answer"
        API.voice_stream(handler, "clip")
        cue, answer = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
        self.assertEqual(cue["kind"], "chime")
        with wave.open(io.BytesIO(base64.b64decode(cue["audio"]))) as sound:
            self.assertEqual(sound.getparams()[:3], (1, 2, OUTPUT_RATE))
            self.assertLess(sound.getnframes(), OUTPUT_RATE // 4)
            self.assertEqual(sound.readframes(sound.getnframes()), CHIME_PCM)
            self.assertTrue(any(CHIME_PCM[:OUTPUT_RATE * 2 // 100]))
        handler.app.engines.synthesize.assert_called_once_with("Five.", "en")
        self.assertEqual(base64.b64decode(answer["audio"]), b"spoken answer")
