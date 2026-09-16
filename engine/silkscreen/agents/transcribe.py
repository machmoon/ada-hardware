"""Speech to text, through the same seam as every other model call.

The desktop app records a spoken board request and needs it back as text.
Gemini accepts inline audio the same way it accepts inline PDFs, so this rides
the existing :class:`~silkscreen.agents.model.Model` protocol -- the audio
travels as a :class:`~silkscreen.agents.model.Document` with an audio MIME
type -- and therefore needs no new key, provider, or SDK surface. Keeping the
call here preserves the layering rule: ``agents/`` is the only place a model
call lives, and :class:`ScriptedModel` keeps the tests offline.
"""

from __future__ import annotations

from .model import Document, Model

__all__ = ["VOCABULARY_MAX_CHARS", "transcribe_audio", "vocabulary_hint"]

#: The stable marker a ScriptedModel keys on, in the same style as the other
#: stage prompts ("designing a printed circuit board", "reviewing a circuit").
_TASK = "transcribing a spoken request"

_PROMPT = (
    f"You are {_TASK} into text.\n"
    "The attached audio is one short clip from a hardware engineer at their "
    "bench. They may say the wake name “Ada” / “hey Ada” and then a board "
    "request, or just a few words. Reply with ONLY the transcript of what was "
    "said: no commentary, no speaker labels, no timestamps, no quotation marks "
    "around the whole answer. Preserve the wake name as “Ada” when that is "
    "what you hear (including near-misses like Aida/Ada). Preserve technical "
    "vocabulary exactly as spoken (part numbers, voltages, units). If you "
    "cannot make out any words, reply with exactly: (inaudible)"
)


#: The budget for the names line, in characters (Gemini exposes no tokenizer
#: here). After wyoming-faster-whisper's ``vocabulary.py``: a biasing prompt
#: is the cheapest accuracy win for proper nouns a model has never heard, and
#: past a small budget it turns into the model echoing names nobody said.
VOCABULARY_MAX_CHARS = 400


def vocabulary_hint(names: list[str] | None) -> str:
    """The names line, filled in the caller's priority order to the budget.

    wyoming-faster-whisper ``RecognitionContext._build_prompt``, applied to
    part numbers instead of rooms: comma-joined, duplicates dropped, stopping
    at the first name that would not fit rather than scoring or sampling, so
    the same run always produces the same prompt. Empty when nothing fits.
    """
    chosen: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = " ".join(str(raw).split())
        if not name or name.lower() in seen:
            continue
        if len(", ".join([*chosen, name])) > VOCABULARY_MAX_CHARS:
            break
        chosen.append(name)
        seen.add(name.lower())
    if not chosen:
        return ""
    return (
        "\nNames and part numbers that may come up, spelled exactly as they "
        "should be written: " + ", ".join(chosen) + ". Use one only when it is "
        "what you actually hear; never add a name that was not said."
    )


def transcribe_audio(
    model: Model,
    audio: bytes,
    mime_type: str,
    *,
    language: str | None = None,
    vocabulary: list[str] | None = None,
) -> str:
    """One audio clip in, its transcript out, stripped of whitespace.

    ``language`` is a hint, not a filter: it tells the model what it is
    probably hearing, which matters for short clips where the accent alone
    is ambiguous. A wrong hint degrades to the model's own judgement.

    Whatever the model says *is* the transcript -- silence, noise, or the
    literal ``(inaudible)`` the prompt asks for all come back verbatim.
    Inventing an empty-is-error rule here would turn a quiet room into a
    server fault; a genuinely failed call already raises ``ModelError``.
    """
    prompt = _PROMPT + vocabulary_hint(vocabulary)
    if language:
        prompt += f"\nThe speech is most likely in this language: {language}"
    text = model.generate(
        prompt,
        documents=[Document(data=audio, mime_type=mime_type)],
        temperature=0.0,
    )
    return text.strip()
