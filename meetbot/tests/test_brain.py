"""Hardy's in-call judgement, offline: scripted model, fake clock, fake voice."""

from __future__ import annotations

import asyncio

from silkscreen.agents.model import ModelError, ScriptedModel

from meetbot.brain import (
    REPLY_MARKER,
    Brain,
    BrainPolicy,
    addressed,
    clean_reply,
    transcript_text,
    voices_doubt,
)
from meetbot.types import SpokenReceipt, Utterance


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.on_sleep = None

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.01)
        if self.on_sleep is not None:
            await self.on_sleep()
        await asyncio.sleep(0)


class Voice:
    def __init__(self, spoken: bool = True) -> None:
        self.said: list[str] = []
        self.spoken = spoken

    async def __call__(self, text: str) -> SpokenReceipt:
        self.said.append(text)
        if self.spoken:
            return SpokenReceipt(True, "played 1.8 s into the call", 1.8)
        return SpokenReceipt(False, "not spoken: the mic control was not found")


async def _inline(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _u(text: str, speaker: str = "Pat", is_self: bool = False) -> Utterance:
    return Utterance(speaker, text, 0.0, 1.0, is_self)


def _brain(model, voice, clock, **policy) -> Brain:
    defaults = {"quiet_s": 1.0, "cooldown_s": 20.0}
    defaults.update(policy)
    return Brain(
        model,
        voice,
        BrainPolicy(**defaults),
        clock=clock,
        sleep=clock.sleep,
        to_thread=_inline,
    )


REPLY = '{"reply": "Fair, I\'ll try it and send you a first pass after the call."}'


# -- the deterministic gates ----------------------------------------------


def test_addressed_matches_the_name_and_caption_misspellings():
    assert addressed("Hardy, what do you think?")
    assert addressed("ok harty can you take that")
    assert addressed("Hardie?")


def test_addressed_ignores_words_that_only_look_like_the_name():
    for text in (
        "this is hard",
        "hardware is hardly the issue",
        "party at six",
        "handy tool",
        "sorry I'm tardy",
    ):
        assert not addressed(text), text


def test_voices_doubt_on_typeset_apostrophes_and_ideas():
    assert voices_doubt("I don’t think this will work")
    assert voices_doubt("that won't work with a coin cell")
    assert voices_doubt("what if we use USB-C instead")
    assert not voices_doubt("I have no idea where the cable went")
    assert not voices_doubt("let's get started")


def test_clean_reply_speaks_whole_sentences_only():
    assert clean_reply(REPLY).startswith("Fair, I'll try it")
    assert clean_reply('Hardy: "Sure thing."') == "Sure thing."
    assert clean_reply("One. Two. Three.") == "One. Two."
    assert clean_reply('{"reply": ""}') == ""
    assert clean_reply('```json\n{"reply": "On it."}\n```') == "On it."
    assert clean_reply("x" * 400) == ""  # a monologue is not cut mid-word
    assert clean_reply('{"reply": 3}') == ""


def test_transcript_text_leaves_hardy_out_by_default():
    lines = [_u("we need a 3.3 volt board"), _u("I'll try it", "Hardy", True)]
    assert transcript_text(lines) == "Pat: we need a 3.3 volt board"
    assert "Hardy: I'll try it" in transcript_text(lines, include_self=True)


# -- the brain -------------------------------------------------------------


def test_doubt_gets_one_spoken_reply_after_the_room_goes_quiet():
    async def go():
        clock, voice = Clock(), Voice()
        model = ScriptedModel(by_marker={REPLY_MARKER: REPLY})
        brain = _brain(model, voice, clock)
        await brain.hear(_u("Let's do a sensor board for the greenhouse."))
        await brain.hear(_u("Actually, I don't think this will work."))
        await brain.settle(timeout_s=5)
        return brain, voice, model

    brain, voice, model = asyncio.run(go())
    assert voice.said == ["Fair, I'll try it and send you a first pass after the call."]
    assert len(brain.replies) == 1
    reply = brain.replies[0]
    assert reply.reason == "doubt" and reply.receipt.spoken
    prompt = model.calls[0]["prompt"]
    assert "greenhouse" in prompt  # the context went with the remark
    assert "Pat: Actually, I don't think this will work." in prompt


def test_never_answers_itself():
    async def go():
        clock, voice = Clock(), Voice()
        model = ScriptedModel(by_marker={REPLY_MARKER: REPLY})
        brain = _brain(model, voice, clock)
        await brain.hear(_u("Hardy, I don't think this will work", "Hardy", True))
        await brain.hear(_u("Hardy here, I don't think that will work", "You"))
        await brain.settle(timeout_s=5)
        return brain, voice, model

    brain, voice, model = asyncio.run(go())
    assert voice.said == [] and model.calls == [] and brain.skipped == []
    assert len(brain.transcript) == 2  # still kept, marked as ours


def test_cooldown_skips_a_second_trigger_without_a_model_call():
    async def go():
        clock, voice = Clock(), Voice()
        model = ScriptedModel(by_marker={REPLY_MARKER: REPLY})
        brain = _brain(model, voice, clock, cooldown_s=30)
        await brain.hear(_u("Hardy, can you own the power stage?"))
        await brain.settle(timeout_s=5)
        clock.t += 5
        await brain.hear(_u("Hardy, and the connector too?"))
        await brain.settle(timeout_s=5)
        return brain, voice, model

    brain, voice, model = asyncio.run(go())
    assert len(voice.said) == 1 and len(model.calls) == 1
    assert "cooldown" in brain.skipped[-1].reason


def test_does_not_talk_over_someone_who_keeps_talking():
    async def go():
        clock, voice = Clock(), Voice()
        model = ScriptedModel(by_marker={REPLY_MARKER: REPLY})
        brain = _brain(model, voice, clock, quiet_s=1.0, max_wait_s=6.0)

        async def keeps_talking():
            await brain.hear(_u("and another thing about the enclosure"))

        await brain.hear(_u("Hardy, I don't think this will work"))
        clock.on_sleep = keeps_talking
        await brain.settle(timeout_s=5)
        return brain, voice, model

    brain, voice, model = asyncio.run(go())
    assert voice.said == [] and model.calls == []
    assert brain.skipped[-1].reason == "the room never went quiet"


def test_waits_for_a_pause_then_answers():
    async def go():
        clock, voice = Clock(), Voice()
        model = ScriptedModel(by_marker={REPLY_MARKER: REPLY})
        brain = _brain(model, voice, clock, quiet_s=1.0)
        remaining = [2]

        async def trailing_words():
            if remaining[0]:
                remaining[0] -= 1
                await brain.hear(_u("...because the battery is tiny"))

        await brain.hear(_u("Hardy, I don't think this will work"))
        clock.on_sleep = trailing_words
        await brain.settle(timeout_s=5)
        return brain, voice

    brain, voice = asyncio.run(go())
    assert len(voice.said) == 1


def test_model_silence_and_model_failure_are_recorded_not_spoken():
    async def go(model):
        clock, voice = Clock(), Voice()
        brain = _brain(model, voice, clock)
        await brain.hear(_u("Hardy, thoughts?"))
        await brain.settle(timeout_s=5)
        return brain, voice

    brain, voice = asyncio.run(
        go(ScriptedModel(by_marker={REPLY_MARKER: '{"reply": ""}'}))
    )
    assert (
        voice.said == [] and brain.skipped[-1].reason == "the model chose to stay quiet"
    )

    class Broken:
        def generate(self, *a, **k):
            raise ModelError("503 from upstream")

    brain, voice = asyncio.run(go(Broken()))
    assert voice.said == [] and "503" in brain.skipped[-1].reason
    assert brain.errors


def test_a_reply_that_did_not_reach_the_room_says_so():
    async def go():
        clock, voice = Clock(), Voice(spoken=False)
        brain = _brain(ScriptedModel(by_marker={REPLY_MARKER: REPLY}), voice, clock)
        await brain.hear(_u("Hardy, can you take this?"))
        await brain.settle(timeout_s=5)
        return brain

    brain = asyncio.run(go())
    assert len(brain.replies) == 1
    assert brain.replies[0].receipt.spoken is False
    assert "mic control" in brain.replies[0].receipt.detail


def test_the_call_ending_cancels_a_reply_still_waiting():
    async def go():
        clock, voice = Clock(), Voice()
        brain = _brain(ScriptedModel(by_marker={REPLY_MARKER: REPLY}), voice, clock)

        async def block():
            await asyncio.sleep(3600)

        clock.on_sleep = block
        await brain.hear(_u("Hardy, one more thing"))
        await asyncio.sleep(0)
        await brain.settle()
        return brain, voice

    brain, voice = asyncio.run(go())
    assert voice.said == []
    assert brain.skipped[-1].reason == "the call ended before a reply"


def test_reply_cap_and_caption_refinements():
    async def go():
        clock, voice = Clock(), Voice()
        brain = _brain(
            ScriptedModel(by_marker={REPLY_MARKER: REPLY}),
            voice,
            clock,
            cooldown_s=0,
            max_replies=1,
        )
        await brain.hear(_u("we need a"))
        await brain.hear(_u("we need a small LDO board"))
        await brain.hear(_u("Hardy, got it?"))
        await brain.settle(timeout_s=5)
        await brain.hear(_u("Hardy, and again?"))
        return brain, voice

    brain, voice = asyncio.run(go())
    assert [u.text for u in brain.transcript][:1] == ["we need a small LDO board"]
    assert len(voice.said) == 1
    assert "reply cap" in brain.skipped[-1].reason
