"""Claude through Claude Code (``SILKSCREEN_CLAUDE_BACKEND=cli``): chosen only
on request, request shape unchanged, the SDK's messages folded into the one
``ModelError`` vocabulary.

Offline except the last test, which spends one real Claude Code call and runs
only when ``SILKSCREEN_LIVE_CLAUDE_CLI=1`` is set beside a signed-in
``claude`` -- the ``test_claude_bedrock.py`` convention.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
from silkscreen.agents.claude import (
    CLAUDE_BACKEND_ENV_VAR,
    CLAUDE_CHEAP_MODEL,
    CLAUDE_MODEL,
    CLI_BACKEND,
    ClaudeModel,
    claude_backend,
    claude_missing,
)
from silkscreen.agents.claude_cli import (
    CREDENTIAL_ENV_VARS,
    MAX_OUTPUT_TOKENS_ENV_VAR,
    AgentSdkClient,
)
from silkscreen.agents.model import CHEAP_MODEL, Document, ModelError
from silkscreen.agents.resilience import provider_is_down

pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
)


def test_cli_is_never_inferred_only_asked_for():
    assert claude_backend({}) is None
    assert claude_backend({CLAUDE_BACKEND_ENV_VAR: "cli"}) == CLI_BACKEND
    both = {CLAUDE_BACKEND_ENV_VAR: "cli", "ANTHROPIC_API_KEY": "k"}
    assert claude_backend(both) == CLI_BACKEND
    assert "claude auth login" in claude_missing({CLAUDE_BACKEND_ENV_VAR: "cli"})


def test_request_shape_and_tiers_are_the_api_ones():
    model = ClaudeModel(env={CLAUDE_BACKEND_ENV_VAR: "cli"}, client=object())
    assert model.backend == CLI_BACKEND
    assert model.model == CLAUDE_MODEL and model.wire_model == CLAUDE_MODEL
    kwargs = model.request_kwargs("hi", system="be brief")
    assert kwargs["system"] == "be brief"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "betas" not in kwargs and "fallbacks" not in kwargs
    cheap = model.for_tier(CHEAP_MODEL)
    assert cheap is not None and cheap.backend == CLI_BACKEND
    assert cheap.model == CLAUDE_CHEAP_MODEL


def _result(**over: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        result="",
        usage={"input_tokens": 2},
    )
    base.update(over)
    return ResultMessage(**base)


@dataclass
class _FakeQuery:
    """Records the prompt and options; replays canned SDK messages."""

    messages: list[Any]
    seen: dict[str, Any] | None = None

    async def __call__(self, *, prompt: Any, options: Any) -> Any:
        sent = [m async for m in prompt]
        self.seen = {"prompt": sent, "options": options}
        for message in self.messages:
            yield message


def _model(fake: _FakeQuery, **kw: Any) -> ClaudeModel:
    client = AgentSdkClient(timeout_s=30, query=fake, cwd="/")
    return ClaudeModel(env={CLAUDE_BACKEND_ENV_VAR: "cli"}, client=client, **kw)


def test_the_sdk_gets_the_same_blocks_and_no_tools():
    fake = _FakeQuery(
        [
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="…", signature=""),
                    TextBlock(text="{}"),
                ],
                model=CLAUDE_MODEL,
                stop_reason="end_turn",
            ),
            _result(result="{}"),
        ]
    )
    text = _model(fake).generate(
        "the question",
        system="JSON only",
        documents=[Document(data=b"%PDF-1.4", mime_type="application/pdf")],
        max_output_tokens=1000,
    )
    assert text == "{}"
    assert fake.seen is not None
    [sent] = fake.seen["prompt"]
    assert sent["type"] == "user"
    blocks = sent["message"]["content"]
    assert [b["type"] for b in blocks] == ["document", "text"]
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    opts = fake.seen["options"]
    assert opts.model == CLAUDE_MODEL
    assert opts.system_prompt == "JSON only"
    assert opts.tools == [] and opts.max_turns == 1
    assert opts.setting_sources == []
    assert opts.thinking == {"type": "adaptive"}
    assert opts.effort == "medium"
    assert opts.env[MAX_OUTPUT_TOKENS_ENV_VAR] == "17000"
    # A stale key in the parent must not outrank the login this backend bills.
    assert all(opts.env[name] == "" for name in CREDENTIAL_ENV_VARS)


def test_a_truncated_answer_is_refused_like_the_api_one():
    fake = _FakeQuery(
        [
            AssistantMessage(
                content=[TextBlock(text="{\"half")], model=CLAUDE_MODEL,
                stop_reason="max_tokens",
            ),
            _result(stop_reason="max_tokens", result="{\"half"),
        ]
    )
    with pytest.raises(ModelError, match="token output budget"):
        _model(fake).generate("q")


def test_a_rate_limit_from_claude_code_parks_the_rung():
    fake = _FakeQuery(
        [
            _result(
                subtype="error_during_execution",
                is_error=True,
                result="You've hit your usage limit. Rate limit resets at 3pm.",
            )
        ]
    )
    with pytest.raises(ModelError) as info:
        _model(fake).generate("q")
    assert "rate_limit_error" in str(info.value)
    assert provider_is_down(str(info.value))


def test_no_result_message_is_an_error_in_words():
    fake = _FakeQuery([])
    with pytest.raises(ModelError, match="without a result"):
        _model(fake).generate("q")


@pytest.mark.skipif(
    os.getenv("SILKSCREEN_LIVE_CLAUDE_CLI") != "1",
    reason="set SILKSCREEN_LIVE_CLAUDE_CLI=1 beside a signed-in `claude`",
)
def test_one_live_claude_code_call():
    model = ClaudeModel(CLAUDE_CHEAP_MODEL, env={CLAUDE_BACKEND_ENV_VAR: "cli"})
    text = model.generate("Reply with the single word CLI-OK.", max_output_tokens=32)
    assert "CLI-OK" in text
