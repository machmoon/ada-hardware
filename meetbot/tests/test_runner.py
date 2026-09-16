"""The kickoff end to end, offline.

A fake room, scripted Gemini, and recorded Slack and engine transports.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from dataclasses import replace
from pathlib import Path

import pytest
from silkscreen.agents.model import ScriptedModel

from meetbot.brain import REPLY_MARKER
from meetbot.clarify import (
    QUESTIONS_MARKER,
    QuestionsError,
    ThreadClient,
    parse_questions,
    wait_for_answer,
)
from meetbot.config import ConfigError, AdaConfig, parse_meet_url
from meetbot.runner import MeetEngineClient, after_call, attend, idea_key, run
from meetbot.types import (
    ADMITTED,
    WAITING_FOR_HOST,
    JoinReceipt,
    SpokenReceipt,
    Utterance,
)
from meetings.intent import BoardRequest
from slackbot.slack import HttpResponse

URL = "https://meet.google.com/abc-defg-hij"
EXTRACT_KEY = "Find every request for a PRINTED CIRCUIT BOARD"

REQUEST_LINE = "we need a small board that runs a soil sensor off a coin cell"
EXTRACTED = json.dumps(
    {
        "requests": [
            {
                "intent": "A coin-cell powered soil moisture sensor board.",
                "quote": REQUEST_LINE,
                "speaker": "Pat",
                "confidence": 0.85,
            }
        ]
    }
)
QUESTIONS = json.dumps(
    {
        "questions": [
            {"question": "Which coin cell, CR2032?", "why": "sets the regulator"},
            {"question": "What connector for the probe?", "why": "footprint"},
        ]
    }
)


def _config(**changes) -> AdaConfig:
    base = AdaConfig(
        google_api_key="k",
        slack_token="xoxb-test",
        slack_channel="UPAT",
        quiet_s=0.0,
        reply_cooldown_s=0.0,
        clarify_wait_s=60.0,
        profile_dir=Path("/nonexistent"),
    )
    return replace(base, **changes)


# -- fakes -------------------------------------------------------------------


class FakeSession:
    def __init__(self, receipt: JoinReceipt) -> None:
        self.receipt = receipt
        self.scripts: list[str] = []
        self.joined: tuple[str, str] | None = None
        self.left = False
        self.ended = asyncio.Event()
        self.end_detail = "the call ended (host ended it for everyone)"
        self.page = object()

    def add_init_script(self, js: str) -> None:
        self.scripts.append(js)

    async def join(self, url: str, display_name: str = "Ada") -> JoinReceipt:
        self.joined = (url, display_name)
        return self.receipt

    async def wait_until_ended(self) -> str:
        await self.ended.wait()
        return "call_ended"

    async def leave(self) -> None:
        self.left = True


class Room:
    """A kickoff: Pat states the board, voices doubt, hears Ada, hangs up."""

    def __init__(self, receipt: JoinReceipt | None = None):
        self.session = FakeSession(receipt or JoinReceipt(ADMITTED, "in the call"))
        self.said: list[str] = []
        self.heard_reply = asyncio.Event()

    def factory(self, profile_dir, headless):
        return self.session

    async def say(self, session, text: str) -> SpokenReceipt:
        assert session is self.session
        self.said.append(text)
        self.heard_reply.set()
        return SpokenReceipt(True, "played into the call", 2.0)

    async def stream(self, session):
        yield Utterance("Pat", "Kickoff time. " + REQUEST_LINE, 0, 1)
        yield Utterance("Pat", "Honestly I don't think this will work.", 1, 2)
        await asyncio.wait_for(self.heard_reply.wait(), 5)
        yield Utterance("Ada", self.said[-1], 2, 3, is_self=True)
        yield Utterance("Pat", "Okay, great, talk soon.", 3, 4)
        session.ended.set()


class RecordedSlack:
    def __init__(self, replies_after: int = 1, answer: str = "CR2032, JST-PH 2 pin"):
        self.requests = []
        self.replies_calls = 0
        self.replies_after = replies_after
        self.answer = answer
        self.fail_post = False

    def __call__(self, request):
        self.requests.append(request)
        method = urllib.parse.urlsplit(request.url).path.rsplit("/", 1)[-1]
        if method == "chat.postMessage":
            if self.fail_post:
                return HttpResponse(200, b'{"ok": false, "error": "channel_not_found"}')
            n = sum(1 for r in self.requests if r.url.endswith("chat.postMessage"))
            return _ok({"channel": "DPAT", "ts": f"100.{n}"})
        if method == "auth.test":
            return _ok({"user_id": "UBOT"})
        if method == "conversations.replies":
            self.replies_calls += 1
            messages = [
                {"user": "UBOT", "bot_id": "B1", "ts": "100.1", "text": "recap"}
            ]
            if self.replies_calls > self.replies_after:
                messages.append({"user": "UPAT", "ts": "100.9", "text": self.answer})
            return _ok({"messages": messages})
        raise AssertionError(f"unexpected Slack call {request.url}")

    def posts(self) -> list[dict]:
        return [
            json.loads(r.body)
            for r in self.requests
            if r.url.endswith("chat.postMessage")
        ]


class RecordedEngine:
    def __init__(self, post_status: int = 201):
        self.requests = []
        self.post_status = post_status

    def __call__(self, request):
        self.requests.append(request)
        path = urllib.parse.urlsplit(request.url).path
        if request.method == "POST" and path == "/inbox":
            if self.post_status == 0:
                raise OSError("connection refused")
            return HttpResponse(
                self.post_status, json.dumps({"id": "idea_" + "a" * 32}).encode()
            )
        if path.startswith("/inbox/"):
            return HttpResponse(
                200, json.dumps({"state": "started", "session": "s1"}).encode()
            )
        if path == "/steps/s1":
            return HttpResponse(
                200, json.dumps({"done": ["propose"], "next": []}).encode()
            )
        raise AssertionError(f"unexpected engine call {request.method} {path}")


def _ok(payload: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps({"ok": True, **payload}).encode())


def _models():
    worker = ScriptedModel(
        by_marker={EXTRACT_KEY: EXTRACTED, QUESTIONS_MARKER: QUESTIONS}
    )
    reply = ScriptedModel(
        by_marker={
            REPLY_MARKER: '{"reply": "Fair, I\'ll try it and send you a first pass."}'
        }
    )
    return worker, reply


def _clients(slack: RecordedSlack, engine: RecordedEngine):
    return (
        ThreadClient("xoxb-test", transport=slack, sleep=lambda s: None),
        MeetEngineClient("http://127.0.0.1:8081", transport=engine),
    )


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


# -- the whole kickoff ------------------------------------------------------


def test_kickoff_speaks_asks_on_slack_folds_the_answer_in_and_hands_off():
    room, (worker, reply) = Room(), _models()
    slack_rec, engine_rec = RecordedSlack(), RecordedEngine()
    slack, engine = _clients(slack_rec, engine_rec)
    config = _config()

    async def go():
        report = await attend(
            URL,
            config,
            reply_model=reply,
            session_factory=room.factory,
            stream=room.stream,
            say=room.say,
            init_scripts=["listen()", "speak()"],
        )
        return report

    report = asyncio.run(go())
    assert room.session.joined == (URL, "Ada")
    assert room.session.scripts == ["listen()", "speak()"]
    assert room.session.left
    assert report.ended.startswith("call_ended")
    assert [r.text for r in report.spoke_aloud] == [
        "Fair, I'll try it and send you a first pass."
    ]

    fake = FakeTime()
    after_call(
        report,
        config,
        model=worker,
        slack=slack,
        engine=engine,
        sleep=fake.sleep,
        clock=fake.clock,
    )

    # Ada's own line never reaches the extractor.
    extract_prompt = next(
        c["prompt"] for c in worker.calls if EXTRACT_KEY in c["prompt"]
    )
    assert "Ada:" not in extract_prompt
    assert [r.intent for r in report.considered] == [
        "A coin-cell powered soil moisture sensor board."
    ]
    assert report.handed_off is report.considered[0]

    posts = slack_rec.posts()
    recap = posts[0]
    assert recap["channel"] == "UPAT" and "thread_ts" not in recap
    assert "I said in the call" in recap["text"]
    assert "Which coin cell, CR2032?" in recap["text"]
    # Every later message is in the thread of the DM channel Slack answered with.
    assert all(p["channel"] == "DPAT" and p["thread_ts"] == "100.1" for p in posts[1:])
    assert all(p.ok for p in report.slack_posts)
    assert report.answer is not None and report.answer.user == "UPAT"

    inbox = json.loads(engine_rec.requests[0].body)
    assert inbox["source"] == "meet"
    assert inbox["reply_to"] == {"channel": "DPAT", "thread_ts": "100.1"}
    assert inbox["key"] == idea_key("abc-defg-hij", report.considered[0])
    assert "CR2032, JST-PH 2 pin" in inbox["text"]
    assert "Which coin cell" in inbox["text"]
    assert report.idea.filed

    # The bridge's follower posted progress into the same thread.
    texts = [p["text"] for p in posts]
    assert any(t.startswith("Done: propose") for t in texts)
    assert any("Every step has run" in t for t in texts)


def test_run_does_nothing_after_a_join_that_was_never_admitted():
    room = Room(JoinReceipt(WAITING_FOR_HOST, "nobody admitted Ada in 300 s"))
    worker, reply = _models()
    slack_rec, engine_rec = RecordedSlack(), RecordedEngine()
    slack, engine = _clients(slack_rec, engine_rec)
    report = asyncio.run(
        run(
            URL,
            _config(),
            model=worker,
            reply_model=reply,
            slack=slack,
            engine=engine,
            session_factory=room.factory,
            stream=room.stream,
            say=room.say,
            init_scripts=[],
        )
    )
    assert not report.join.admitted and room.session.left
    assert slack_rec.requests == [] and engine_rec.requests == [] and worker.calls == []
    assert "not in the call" in report.warnings[0]


def _report_with_speech() -> object:
    from meetbot.runner import CallReport

    return CallReport(
        meeting_code="abc-defg-hij",
        join=JoinReceipt(ADMITTED, "in"),
        transcript=[Utterance("Pat", REQUEST_LINE, 0, 1)],
    )


def test_undelivered_recap_is_a_warning_and_nothing_waits_on_it():
    worker, _ = _models()
    slack_rec, engine_rec = RecordedSlack(), RecordedEngine()
    slack_rec.fail_post = True
    slack, engine = _clients(slack_rec, engine_rec)
    report = after_call(
        _report_with_speech(),
        _config(),
        model=worker,
        slack=slack,
        engine=engine,
        sleep=lambda s: None,
    )
    assert report.slack_posts[0].ok is False
    assert any("channel_not_found" in w for w in report.warnings)
    assert not any(r.url.endswith("conversations.replies") for r in slack_rec.requests)
    # The build still reaches the laptop, with no thread to report into.
    assert json.loads(engine_rec.requests[0].body)["reply_to"] == {}


def test_no_answer_in_time_holds_the_build_when_told_to():
    worker, _ = _models()
    slack_rec, engine_rec = RecordedSlack(replies_after=10_000), RecordedEngine()
    slack, engine = _clients(slack_rec, engine_rec)
    fake = FakeTime()
    report = after_call(
        _report_with_speech(),
        _config(build_on_timeout=False),
        model=worker,
        slack=slack,
        engine=engine,
        sleep=fake.sleep,
        clock=fake.clock,
        poll_s=5,
    )
    assert report.answer is None and report.idea is None and engine_rec.requests == []
    assert "holding off" in slack_rec.posts()[-1]["text"]
    assert fake.t <= 60


def test_engine_down_is_said_in_the_thread_not_claimed_as_started():
    worker, _ = _models()
    slack_rec, engine_rec = RecordedSlack(), RecordedEngine(post_status=0)
    slack, engine = _clients(slack_rec, engine_rec)
    fake = FakeTime()
    report = after_call(
        _report_with_speech(),
        _config(),
        model=worker,
        slack=slack,
        engine=engine,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert report.idea.status == 0 and not report.idea.filed
    assert report.handed_off is None
    assert "silkscreen serve" in slack_rec.posts()[-1]["text"]


def test_low_confidence_request_is_recorded_not_built_and_no_questions_asked():
    low = EXTRACTED.replace("0.85", "0.3")
    worker = ScriptedModel(by_marker={EXTRACT_KEY: low})
    slack_rec, engine_rec = RecordedSlack(), RecordedEngine()
    slack, engine = _clients(slack_rec, engine_rec)
    report = after_call(
        _report_with_speech(),
        _config(),
        model=worker,
        slack=slack,
        engine=engine,
        sleep=lambda s: None,
    )
    assert len(report.considered) == 1 and report.handed_off is None
    assert engine_rec.requests == [] and len(worker.calls) == 1
    assert "below the 0.60" in slack_rec.posts()[0]["text"]


def test_invented_quote_is_dropped_by_the_meetings_filter():
    invented = EXTRACTED.replace(REQUEST_LINE, "please build me a 48 volt motor driver")
    worker = ScriptedModel(by_marker={EXTRACT_KEY: invented})
    report = after_call(
        _report_with_speech(), _config(), model=worker, slack=None, engine=None
    )
    assert report.considered == []
    assert any("unverifiable" in w for w in report.warnings)


# -- the pieces --------------------------------------------------------------


def test_questions_are_validated_as_a_batch():
    assert parse_questions('{"questions": []}') == []
    with pytest.raises(QuestionsError) as caught:
        parse_questions(
            json.dumps(
                {
                    "questions": [{"question": ""}, {"question": 3}]
                    + [{"question": f"q{i}"} for i in range(6)]
                }
            )
        )
    assert len(caught.value.errors) == 3


def test_questions_repair_once_then_give_up_loudly():
    from meetbot.clarify import propose_questions

    model = ScriptedModel(responses=["not json", "still not json"])
    request = BoardRequest("a board", REQUEST_LINE, 0.9, "Pat")
    result = propose_questions(model, "Pat: " + REQUEST_LINE, [request])
    assert result.questions is None and result.warnings and len(model.calls) == 2
    assert "rejected" in model.calls[1]["prompt"]
    empty = propose_questions(ScriptedModel(), "", [])
    assert empty.asked_model is False


def test_wait_for_answer_ignores_bots_and_other_users():
    slack_rec = RecordedSlack(replies_after=0)
    slack = ThreadClient("xoxb", transport=slack_rec)
    fake = FakeTime()
    got = wait_for_answer(
        slack,
        "DPAT",
        "100.1",
        after_ts="100.1",
        timeout_s=30,
        only_user="USOMEONE",
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert got is None
    got = wait_for_answer(
        slack,
        "DPAT",
        "100.1",
        after_ts="100.1",
        timeout_s=30,
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert got.text == "CR2032, JST-PH 2 pin"
    replies = [r for r in slack_rec.requests if "conversations.replies" in r.url]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(replies[0].url).query)
    assert replies[0].method == "GET"
    assert query["channel"] == ["DPAT"] and query["ts"] == ["100.1"]
    assert replies[0].headers["Authorization"] == "Bearer xoxb"


def test_config_names_every_missing_variable_at_once():
    with pytest.raises(ConfigError) as caught:
        AdaConfig.from_env({})
    message = str(caught.value)
    for name in ("GOOGLE_API_KEY", "SLACK_BOT_TOKEN", "HARDY_SLACK_CHANNEL"):
        assert name in message
    config = AdaConfig.from_env({"GOOGLE_API_KEY": "k"}, require_slack=False)
    assert not config.slack_enabled and config.confidence_floor == 0.6
    with pytest.raises(ConfigError, match="HARDY_REPLY_COOLDOWN_S"):
        AdaConfig.from_env(
            {"GOOGLE_API_KEY": "k", "HARDY_REPLY_COOLDOWN_S": "soon"},
            require_slack=False,
        )
    assert "xoxb" not in json.dumps(
        AdaConfig.from_env(
            {
                "GOOGLE_API_KEY": "k",
                "SLACK_BOT_TOKEN": "xoxb-secret",
                "HARDY_SLACK_CHANNEL": "C1",
            }
        ).redacted()
    )


def test_meet_url_is_exact_host_with_a_meeting_code():
    assert parse_meet_url(URL) == "abc-defg-hij"
    assert parse_meet_url("meet.google.com/abc-defg-hij") == "abc-defg-hij"
    for bad in (
        "https://meet.google.com@evil.example/abc-defg-hij",
        "http://meet.google.com/abc-defg-hij",
        "https://meet.google.com/new",
    ):
        with pytest.raises(ConfigError):
            parse_meet_url(bad)
