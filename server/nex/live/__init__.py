"""Shared audio format and detector labels for the Excalibur live voice service."""

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
PLAYBACK_LABELS = frozenset({"Television", "Radio"})
VOICE_LABELS = frozenset({"Speech", "Conversation", "Narration, monologue", "Speech synthesizer"})
