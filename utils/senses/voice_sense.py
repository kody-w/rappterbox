"""VOICE — the same response, said aloud.

A sense that translates the main reply into a short, conversational
spoken-word version suitable for text-to-speech. Frontends with a TTS
backend (browser SpeechSynthesis, Azure Speech, ElevenLabs) read
voice_response from the chat envelope and speak it.

This is one of two bundled senses. Drop-in single-file: rename, change
the delimiter and prompt, and you have a new sense. Loss-tolerant: if
this file is removed, the rest of the brainstem keeps running and the
voice channel just goes silent.
"""

name = "voice"
delimiter = "|||VOICE|||"
response_key = "voice_response"
wrapper_tag = "voice"
system_prompt = (
    "After your main reply, append `|||VOICE|||` followed by a concise, "
    "conversational version of your answer suitable for text-to-speech. "
    "Keep the voice version under 2-3 sentences. Plain text, no markdown. "
    "The part before |||VOICE||| should be the full formatted response."
)
