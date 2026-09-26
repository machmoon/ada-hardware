"""Alexa+ front end: Ada's voice toolset, as MCP tools that answer at once.

A person talks to an Alexa+ agent; the agent calls these six tools over MCP
(Streamable HTTP, 2025-11-25); Ada plans, drafts, places, routes and reviews
a printed circuit board in the background, and every tool answers with one
pre-written sentence to speak plus the structured data behind it:

* ``start_board_design`` -- returns at once; planning runs on a worker
* ``answer_design_questions`` -- one question at a time, or "you choose"
* ``continue_design`` -- place, route and review, in the background
* ``board_status`` -- where it stands, and the one question Ada needs answered
* ``explain_finding`` -- one review finding, in plain speech
* ``recall_my_boards`` -- this account's boards, across conversations

Two boundaries, stated rather than discovered:

* **Simulated Alexa+.** Nothing here is an Alexa skill or talks to Amazon;
  it is the MCP server an Alexa+ agent would call. **Unverified live**: no
  Alexa+ agent has ever called it, and the only independent clients it has
  met are python-sdk's.
* **It drafts and checks; it never orders.** No tool reaches a fab, a
  distributor or a payment path, and the failure speech says "Nothing was
  ordered" because a person hearing "it failed" deserves to know that.

Speech never claims KiCad checked a board: the hosted stack has none
(reviewer finding M5). The check is "the design review".

``python -m alexabot.sim`` is the **simulated Alexa+ experience**: a voice
page, a Strands agent per conversation (Amazon Nova 2 Lite on Bedrock, or a
rule-based ``--scripted`` stand-in) as the MCP client of these tools, and
Amazon Polly or the browser for Ada's voice (:mod:`alexabot.sim`,
:mod:`alexabot.agent`). It is labelled "Simulated Alexa+ experience" on
screen and is not Alexa, an Alexa skill, or made by Amazon. Strands and boto3
are the ``alexa`` extra, imported lazily.

Structured like ``slackbot/`` and ``zoombot/``: standard library only, no
engine logic of its own, and :mod:`alexabot.runner` is the only module that
imports the pipeline (``service.steps``), lazily, at startup. The design and
its sources are in ``docs/alexa.md``.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """``python -m alexabot``; imported lazily so ``import alexabot`` is cheap."""
    from .app import main as run

    return run(argv)
