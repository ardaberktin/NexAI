import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


@unittest.skipUnless(importlib.util.find_spec("pipecat"), "requires the live speech virtual environment")
class TurnAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_packet_sizes_preserve_preroll_and_final_partial_frame(self):
        for packet_size in (194, 640, 4096):
            with self.subTest(packet_size=packet_size):
                await self.check_packet_size(packet_size)

    async def check_packet_size(self, packet_size):
        from nex.live.turn_audio import TurnAudio
        from pipecat.audio.vad.vad_analyzer import VADState

        frame = 512 * 2
        pcm = bytes(frame * 3) + bytes([1, 0]) * (512 * 2 + 39)
        samples = []

        def feed(audio):
            samples.append(bytes(audio))
            return "Yes, please"

        vad = Mock()
        vad.num_frames_required.return_value = 512
        vad.analyze_audio = AsyncMock(side_effect=lambda audio: VADState.SPEAKING if audio[0] else VADState.QUIET)
        listener = SimpleNamespace(vad=vad, smart=Mock(), begin=Mock(), feed=feed,
                                   finish=Mock(return_value="Yes, please"))
        started, finished = AsyncMock(), AsyncMock()
        turn = TurnAudio(listener, started, finished, None)
        for offset in range(0, len(pcm), packet_size):
            await turn.receive(pcm[offset:offset + packet_size])
        await turn.flush()
        started.assert_awaited_once()
        finished.assert_awaited_once()
        identifier, start = started.await_args.args
        finished_id, _, end, audio, transcript = finished.await_args.args
        self.assertEqual((identifier, start), (finished_id, 0))
        self.assertEqual((end, audio, transcript), (len(pcm) // 2, pcm, "Yes, please"))
        self.assertEqual(b"".join(samples), pcm)
        self.assertEqual([len(call.args[0]) for call in vad.analyze_audio.await_args_list], [frame] * 5)
