"""Use room state and the local model to recognize an unnamed conversational follow-up."""
import asyncio

from ..conversation_history import prompt_history
from ..wake import next_wake_body, spoken_wake_body
from .attention import addressed, confirm_wake_transcript


class FollowupGate:
    def __init__(self, attention):
        self.attention = attention
        self.expects_answer = False
        self.recent = []

    async def resolve(self, text, audio, context, language, *, history=()):
        text = await confirm_wake_transcript(self.attention.verifier, audio, language, text)
        if (body := spoken_wake_body(text)) is not None:
            return body.strip()
        if not text.strip():
            return ""
        room = self.attention.room
        if room and await asyncio.to_thread(room.busy, bytes(audio), context):
            return ""
        conversation = prompt_history(history)
        if self.expects_answer:
            conversation.append({"role": "assistant", "content": "Waiting for the user's answer."})
        result = await addressed(self.attention.dialogue, text, self.recent, conversation)
        self.recent = [] if result else [text]
        return (next_wake_body(text) or text) if result else ""
