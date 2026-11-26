"""Shared audio format and detector labels for the Excalibur live voice service."""

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
PLAYBACK_LABELS = frozenset({"Television", "Radio"})
MEDIA_CUE_LABELS = frozenset({
    "Narration, monologue", "Music", "Background music", "Video game music",
    "Speech synthesizer", "Applause", "Cheering", "Crowd", "Laughter",
})
VOICE_LABELS = frozenset({"Speech", "Conversation", "Narration, monologue", "Speech synthesizer"})
