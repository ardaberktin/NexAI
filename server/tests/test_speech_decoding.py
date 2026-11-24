import tempfile
import unittest
from pathlib import Path

from nex.transcription import read_whisper_transcript


class SpeechDecodingTests(unittest.TestCase):
    def test_a_broken_token_does_not_discard_other_decodable_speech(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "output.json"
            path.write_bytes(
                b'{"transcription":['
                b'{"text":"I am fine.","tokens":[{"text":"\xe1","p":0.8},{"text":"fine","p":0.97}]},'
                b'{"text":"\xe1 here","tokens":[{"text":"here","p":0.99}]}]}'
            )
            self.assertEqual(read_whisper_transcript(path), "I am fine.")

    def test_invalid_json_still_reports_a_decoder_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "output.json"
            path.write_bytes(b'{"transcription":')
            with self.assertRaises(ValueError):
                read_whisper_transcript(path)
