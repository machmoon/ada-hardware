"""Ada in Jira, offline: a recorded transport stands in for the Jira site."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from googleapps.transport import HttpResponse
from jirabot.app import Dispatcher, run_ticket
from jirabot.jira import Config, JiraClient, JiraError, Trigger, parse_event

CONFIG = Config(
    base_url="https://acme.atlassian.net",
    email="ada@acme.com",
    api_token="tok",
    webhook_secret="s3cret",
    account_id="ada-acct",
)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


def labelled(to="bug ada", frm="bug", user="u1"):
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "HW-7"},
        "user": {"accountId": user, "displayName": "Sam"},
        "changelog": {
            "items": [{"field": "labels", "fromString": frm, "toString": to}]
        },
    }


def commented(body, author="u1"):
    return {
        "webhookEvent": "comment_created",
        "issue": {"key": "HW-7"},
        "comment": {
            "body": body,
            "author": {"accountId": author, "displayName": "Sam"},
        },
    }


def test_the_three_devin_triggers_and_what_they_skip():
    assert parse_event(CONFIG, labelled()) == Trigger("HW-7", "label", "Sam")
    assigned = labelled()
    assigned["changelog"]["items"] = [{"field": "assignee", "to": "ada-acct"}]
    assert parse_event(CONFIG, assigned).how == "assigned"
    t = parse_event(CONFIG, commented("@Ada use a USB-C connector please"))
    assert (t.how, t.instructions) == ("mention", "use a USB-C connector please")
    # Label already present, a longer handle, Ada's own comment, other events.
    assert isinstance(parse_event(CONFIG, labelled(to="ada", frm="ada")), str)
    assert isinstance(parse_event(CONFIG, commented("ping @ada-team")), str)
    assert "own service account" in parse_event(
        CONFIG, commented("@ada again", author="ada-acct")
    )
    assert isinstance(parse_event(CONFIG, {"webhookEvent": "issue_deleted"}), str)


def test_config_refuses_a_non_atlassian_or_plain_http_site():
    with pytest.raises(JiraError):
        Config(
            base_url="http://acme.atlassian.net",
            email="a",
            api_token="t",
            webhook_secret="s",
        )
    with pytest.raises(JiraError):
        Config(
            base_url="https://evil.example.com",
            email="a",
            api_token="t",
            webhook_secret="s",
        )


class FakeJira:
    def __init__(self, description="A 3.3 V LDO board from USB"):
        self.requests = []
        self.description = description

    def __call__(self, request):
        self.requests.append(request)
        if request.method == "GET":
            return HttpResponse(
                200,
                json.dumps(
                    {
                        "fields": {
                            "summary": "LDO board",
                            "description": self.description,
                        }
                    }
                ).encode(),
            )
        return HttpResponse(201, b"{}")

    def comments(self):
        return [
            json.loads(r.body)["body"]
            for r in self.requests
            if r.url.endswith("/comment")
        ]


def fake_result():
    return SimpleNamespace(
        summary=lambda: "4 parts, 5 nets",
        blockers=[],
        review=SimpleNamespace(ok=True),
        route=SimpleNamespace(unrouted={"VBUS": "blocked by U1"}),
    )


def test_a_run_comments_twice_and_attaches_the_project():
    jira = FakeJira()
    seen = {}

    def build(intent, output):
        seen["intent"] = intent
        output.parent.mkdir(parents=True)
        output.write_text("(kicad_pcb)")
        return fake_result()

    run_ticket(
        JiraClient(CONFIG, jira), Trigger("HW-7", "mention", "Sam", "add a fuse"), build
    )
    assert seen["intent"] == (
        "LDO board\n\nA 3.3 V LDO board from USB\n\n"
        "Additional instructions from Sam: add a fuse"
    )
    start, report = jira.comments()
    assert "designing" in start and "fresh run" in start
    assert "VBUS: blocked by U1" in report and "HW-7-ada.zip" in report
    upload = next(r for r in jira.requests if r.url.endswith("/attachments"))
    assert upload.headers["X-Atlassian-Token"] == "no-check"
    assert all(
        r.url.startswith("https://acme.atlassian.net/rest/api/2/")
        for r in jira.requests
    )
    zipped = upload.body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    assert zipfile.ZipFile(io.BytesIO(zipped)).namelist() == ["HW-7/board.kicad_pcb"]


def test_a_failed_run_says_so_on_the_ticket():
    jira = FakeJira()

    def build(intent, output):
        raise RuntimeError("model ladder exhausted")

    run_ticket(JiraClient(CONFIG, jira), Trigger("HW-7", "label", "Sam"), build)
    assert "could not finish" in jira.comments()[-1]
    assert "model ladder exhausted" in jira.comments()[-1]


def test_dispatcher_checks_signature_and_drops_a_redelivery():
    ran = []
    dispatcher = Dispatcher(
        CONFIG,
        JiraClient(CONFIG, FakeJira()),
        build=lambda intent, output: ran.append(intent) or fake_result(),
    )
    body = json.dumps(labelled()).encode()
    assert dispatcher.handle(body, "sha256=00")[0] == 401
    assert dispatcher.handle(body, sign(body))[0] == 202
    assert dispatcher.handle(body, sign(body)) == (
        200,
        {"skipped": "duplicate delivery"},
    )
    for t in dispatcher.threads:
        t.join(5)
    assert len(ran) == 1
