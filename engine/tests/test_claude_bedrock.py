"""Claude on Amazon Bedrock: chosen only on request, named by inference profile.

Offline except the last test, which spends one real Bedrock call and runs only
when ``SILKSCREEN_LIVE_BEDROCK=1`` is set beside AWS credentials -- the
``test_live_model.py`` convention, one notch stricter because AWS credentials
sit on most developer machines for other reasons.
"""

from __future__ import annotations

import os

import pytest
from silkscreen.agents.claude import (
    BEDROCK_MODEL_ENV_VAR,
    BEDROCK_REGION_ENV_VAR,
    CLAUDE_BACKEND_ENV_VAR,
    CLAUDE_CHEAP_MODEL,
    CLAUDE_MODEL,
    ClaudeModel,
    claude_backend,
    claude_missing,
)
from silkscreen.agents.model import CHEAP_MODEL, ModelError


def test_bedrock_is_never_inferred_from_aws_credentials_alone():
    env = {
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "y",
    }
    assert claude_backend(env) is None
    assert claude_backend({**env, "ANTHROPIC_API_KEY": "k"}) == "api"


def test_bedrock_needs_the_switch_and_a_region():
    assert claude_backend({CLAUDE_BACKEND_ENV_VAR: "bedrock"}) is None
    assert "AWS_REGION" in claude_missing({CLAUDE_BACKEND_ENV_VAR: "bedrock"})
    env = {CLAUDE_BACKEND_ENV_VAR: "bedrock", BEDROCK_REGION_ENV_VAR: "us-east-1"}
    assert claude_backend(env) == "bedrock"


def test_the_wire_id_is_the_inference_profile_and_the_logical_id_stays():
    env = {CLAUDE_BACKEND_ENV_VAR: "bedrock", BEDROCK_REGION_ENV_VAR: "us-east-1"}
    model = ClaudeModel(env=env, client=object())
    assert model.backend == "bedrock"
    assert model.model == CLAUDE_MODEL
    assert model.wire_model == f"global.anthropic.{CLAUDE_MODEL}"
    kwargs = model.request_kwargs("hi")
    assert kwargs["model"] == model.wire_model
    assert "betas" not in kwargs and "fallbacks" not in kwargs
    cheap = model.for_tier(CHEAP_MODEL)
    assert cheap is not None and cheap.backend == "bedrock"
    assert cheap.model == CLAUDE_CHEAP_MODEL
    assert cheap.wire_model == f"global.anthropic.{CLAUDE_CHEAP_MODEL}"


def test_the_override_applies_to_the_primary_tier_only():
    env = {
        CLAUDE_BACKEND_ENV_VAR: "bedrock",
        BEDROCK_REGION_ENV_VAR: "us-east-1",
        BEDROCK_MODEL_ENV_VAR: "us.anthropic.claude-opus-5",
    }
    model = ClaudeModel(env=env, client=object())
    assert model.wire_model == "us.anthropic.claude-opus-5"
    assert (
        model.for_tier(CHEAP_MODEL).wire_model
        == f"global.anthropic.{CLAUDE_CHEAP_MODEL}"
    )


def test_api_and_vertex_keep_the_logical_id_on_the_wire():
    model = ClaudeModel(env={"ANTHROPIC_API_KEY": "k"}, client=object())
    assert model.wire_model == CLAUDE_MODEL


def test_a_non_claude_id_is_still_refused_on_bedrock():
    env = {CLAUDE_BACKEND_ENV_VAR: "bedrock", BEDROCK_REGION_ENV_VAR: "us-east-1"}
    with pytest.raises(ModelError, match="non-Claude"):
        ClaudeModel("gemini-3.7-flash", env=env, client=object())


@pytest.mark.skipif(
    os.getenv("SILKSCREEN_LIVE_BEDROCK") != "1" or not os.getenv("AWS_REGION"),
    reason="set SILKSCREEN_LIVE_BEDROCK=1 and AWS_REGION with AWS credentials",
)
def test_one_live_bedrock_call():
    model = ClaudeModel(
        CLAUDE_CHEAP_MODEL,
        env={
            CLAUDE_BACKEND_ENV_VAR: "bedrock",
            BEDROCK_REGION_ENV_VAR: os.environ["AWS_REGION"],
        },
    )
    text = model.generate(
        "Reply with the single word BEDROCK-OK.", max_output_tokens=32
    )
    assert "BEDROCK-OK" in text
