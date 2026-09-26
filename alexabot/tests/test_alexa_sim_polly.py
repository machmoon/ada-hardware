"""Amazon Polly behind a cache and a budget (``alexabot/polly.py``), with a
fake client: no boto3, no network."""

import io

import pytest

from alexabot import polly


class FakePolly:
    def __init__(self, fail=None, billed="42"):
        self.calls = []
        self.fail = fail
        self.billed = billed

    def synthesize_speech(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return {"AudioStream": io.BytesIO(b"ID3fake-mp3"),
                "ContentType": "audio/mpeg",
                "ResponseMetadata": {"HTTPHeaders": {
                    "x-amzn-requestcharacters": self.billed}}}


def test_request_is_neural_mp3_24k_plain_text_joanna():
    client = FakePolly()
    audio = polly.PollySpeaker(client).synthesize("Placing the parts now.")
    assert client.calls == [{"Engine": "neural", "OutputFormat": "mp3",
                             "SampleRate": "24000", "Text": "Placing the parts now.",
                             "TextType": "text", "VoiceId": "Joanna"}]
    assert audio.data == b"ID3fake-mp3" and audio.content_type == "audio/mpeg"


def test_same_text_is_synthesised_once():
    client = FakePolly()
    speaker = polly.PollySpeaker(client)
    first = speaker.synthesize("Hello there.")
    again = speaker.synthesize("Hello there.")
    assert len(client.calls) == 1
    assert again.cached and again.data == first.data


def test_budget_refuses_before_calling():
    client = FakePolly()
    left = iter([True, False])
    speaker = polly.PollySpeaker(client, take=lambda: next(left))
    speaker.synthesize("One.")
    with pytest.raises(polly.PollyBudgetSpent) as caught:
        speaker.synthesize("Two.")
    assert caught.value.reason == "polly_budget"
    assert len(client.calls) == 1


def test_text_over_3000_chars_is_refused():
    client = FakePolly()
    with pytest.raises(polly.TextTooLong):
        polly.PollySpeaker(client).synthesize("a" * 3001)
    assert client.calls == []


def test_errors_become_unavailable_with_class_name_only():
    class ThrottlingException(Exception):
        pass

    client = FakePolly(fail=ThrottlingException("secret detail https://x?key=1"))
    with pytest.raises(polly.PollyUnavailable) as caught:
        polly.PollySpeaker(client).synthesize("Hi.")
    assert str(caught.value) == "ThrottlingException"
    assert caught.value.reason == "polly_unavailable"


def test_billed_characters_are_read_from_the_header():
    audio = polly.PollySpeaker(FakePolly(billed="17")).synthesize("Seventeen chars.")
    assert audio.billed_chars == 17
    unknown = polly.PollySpeaker(FakePolly(billed="")).synthesize("x")
    assert unknown.billed_chars is None
