"""Create, check or delete the Amazon Bedrock AgentCore Memory resource in
which the simulated Alexa+ agent remembers design preferences.

    python scripts/aws/agentcore_memory.py create    [--region us-east-1]
    python scripts/aws/agentcore_memory.py status    [--region us-east-1]
    python scripts/aws/agentcore_memory.py teardown  [--yes] [--region us-east-1]

``create`` is idempotent: it looks for the resource by its exact name first
(``ListMemories`` has no name field, so the id, ``<name>-<10 characters>``, is
the only handle, matched whole: the AgentCore SDK's ``create_or_get_memory``
matches by prefix, so ``AdaDesignPreferences`` would also find
``AdaDesignPreferencesOld-...``). Otherwise it calls ``CreateMemory`` with one
built-in user-preference strategy and no execution role, then botocore's own
``MemoryCreated`` waiter (at most six ``GetMemory`` calls, 30 s apart), and
prints ``export ADA_AGENTCORE_MEMORY_ID=<id>``. Still creating after that:
exit 3, "run status in a minute". ``teardown`` without ``--yes`` only says
what it would delete.

It prints the region, name, id and status, never a credential, an ARN or the
account number; an AWS error is printed as its code and a sentence, never its
message (which carries ARNs).

IAM: ``bedrock-agentcore:CreateMemory``, ``ListMemories``, ``GetMemory`` and
``DeleteMemory``. The constants (name, strategy, namespace, expiry) are
imported from ``alexabot/memory.py``, so this script and the sim's startup
probe cannot disagree.

Sources: botocore ``bedrock-agentcore-control/2023-06-05/service-2.json`` and
``waiters-2.json`` (develop ``86201a3``); awslabs/amazon-bedrock-agentcore-
samples ``e1a55b3`` ``.../02-long-term-memory/01-built-in-strategies/
user-preference.py`` (the boto3 create path, no execution role).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alexabot.memory import (  # noqa: E402 -- after the repo root is importable
    EVENT_EXPIRY_DAYS,
    MEMORY_DESCRIPTION,
    MEMORY_NAME,
    REASONS,
    STRATEGY,
    probe,
    reason_for,
)

OWN_ID = re.compile(rf"\A{MEMORY_NAME}-[a-zA-Z0-9]{{10}}\Z")
WAIT = {"Delay": 30, "MaxAttempts": 6}
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_CREATING = 0, 1, 2, 3
_SENTENCES = {
    "AccessDeniedException": "this AWS identity may not manage AgentCore memories",
    "ResourceNotFoundException": "the memory resource was not found",
    "ServiceQuotaExceededException": "this account has reached its AgentCore "
                                     "memory quota",
    "ConflictException": "a memory with this name is already being created or "
                         "deleted; run status in a minute",
    "ValidationException": "AgentCore refused the request's shape",
}


def error_words(exc: BaseException) -> str:
    """``exc`` as its code and a sentence; never its message."""
    response = getattr(exc, "response", None)
    code = ""
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "")
    code = code or type(exc).__name__
    return f"{code}: {_SENTENCES.get(code) or REASONS[reason_for(exc)]}"


def find(control: Any) -> dict[str, Any] | None:
    """The resource whose id is exactly ``<MEMORY_NAME>-<10 characters>``."""
    for page in control.get_paginator("list_memories").paginate():
        for summary in page.get("memories") or []:
            if OWN_ID.match(str(summary.get("id") or "")):
                return summary
    return None


def show(memory: dict[str, Any], region: str, out: TextIO) -> None:
    strategies = []
    for s in memory.get("strategies") or []:
        spaces = s.get("namespaceTemplates") or s.get("namespaces") or []
        strategies.append(f"{s.get('type')} {s.get('name')} -> {', '.join(spaces)}")
    print(f"region:   {region}", file=out)
    print(f"name:     {memory.get('name') or MEMORY_NAME}", file=out)
    print(f"id:       {memory.get('id')}", file=out)
    print(f"status:   {memory.get('status')}", file=out)
    print(f"strategy: {'; '.join(strategies) or 'none'}", file=out)


class _Answered:
    """A ``GetMemory`` answer already made, for :func:`alexabot.memory.probe`."""

    def __init__(self, memory: dict[str, Any]) -> None:
        self.memory = memory

    def get_memory(self, **_: Any) -> dict[str, Any]:
        return {"memory": self.memory}


def _export(memory_id: str, out: TextIO) -> None:
    print(f"export ADA_AGENTCORE_MEMORY_ID={memory_id}", file=out)


def _exit_for(status: str | None) -> int:
    if status == "ACTIVE":
        return EXIT_OK
    return EXIT_CREATING if status == "CREATING" else EXIT_FAILED


def create(control: Any, region: str, out: TextIO,
           wait: dict[str, int] | None = None) -> int:
    found = find(control)
    if found is not None:
        memory = control.get_memory(memoryId=found["id"])["memory"]
        print(f"{MEMORY_NAME} already exists; nothing created.", file=out)
        show(memory, region, out)
        # The sim's own startup check, on the answer already in hand.
        for problem in probe(_Answered(memory), found["id"]):
            print(f"problem:  {problem}", file=out)
        _export(memory["id"], out)
        return _exit_for(memory.get("status"))
    created = control.create_memory(
        clientToken=str(uuid.uuid4()), name=MEMORY_NAME, description=MEMORY_DESCRIPTION,
        eventExpiryDuration=EVENT_EXPIRY_DAYS, memoryStrategies=[STRATEGY])["memory"]
    memory_id = created["id"]
    print(f"Created {MEMORY_NAME} in {region}; waiting for it to become ACTIVE "
          "(up to 3 minutes).", file=out)
    from botocore.exceptions import WaiterError

    try:
        control.get_waiter("memory_created").wait(memoryId=memory_id,
                                                  WaiterConfig=wait or WAIT)
    except WaiterError as exc:
        last = (getattr(exc, "last_response", None) or {}).get("memory") or {}
        status = last.get("status") or created.get("status")
        show({**created, "status": status}, region, out)
        if status == "FAILED":
            print("AgentCore could not create the memory; see the AWS console.",
                  file=out)
            return EXIT_FAILED
        print("Still creating; run `python scripts/aws/agentcore_memory.py status` in "
              "a minute.", file=out)
        _export(memory_id, out)
        return EXIT_CREATING
    show({**created, "status": "ACTIVE"}, region, out)
    _export(memory_id, out)
    return EXIT_OK


def status(control: Any, region: str, out: TextIO) -> int:
    found = find(control)
    if found is None:
        print(f"{MEMORY_NAME} does not exist in {region}; run create.", file=out)
        return EXIT_FAILED
    memory = control.get_memory(memoryId=found["id"])["memory"]
    show(memory, region, out)
    _export(memory["id"], out)
    return _exit_for(memory.get("status"))


def teardown(control: Any, region: str, out: TextIO, *, yes: bool) -> int:
    found = find(control)
    if found is None:
        print(f"{MEMORY_NAME} does not exist in {region}; nothing to delete.", file=out)
        return EXIT_OK
    if not yes:
        print(f"Would delete {MEMORY_NAME} ({found['id']}) in {region}, with every "
              "preference it holds. Run again with --yes to delete it.", file=out)
        return EXIT_FAILED
    answer = control.delete_memory(memoryId=found["id"], clientToken=str(uuid.uuid4()))
    print(f"Deleting {MEMORY_NAME} ({answer.get('memoryId') or found['id']}) in "
          f"{region}: {answer.get('status') or 'requested'}.", file=out)
    print("Unset ADA_AGENTCORE_MEMORY_ID; the sim will say memory is off.", file=out)
    return EXIT_OK


def control_client(region: str) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-agentcore-control", region_name=region,
                        config=Config(retries={"mode": "standard", "max_attempts": 3},
                                      connect_timeout=5, read_timeout=20))


def main(argv: list[str] | None = None, *, control: Any = None,
         out: TextIO | None = None, wait: dict[str, int] | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="python scripts/aws/agentcore_memory.py",
        description="Create, check or delete Ada's AgentCore Memory resource.")
    parser.add_argument("action", choices=("create", "status", "teardown"))
    parser.add_argument("--region", default=None)
    parser.add_argument("--yes", action="store_true",
                        help="teardown only: really delete it")
    args = parser.parse_args(argv)
    region = (args.region or os.environ.get("ADA_AGENTCORE_REGION")
              or os.environ.get("AWS_REGION") or "us-east-1")
    try:
        client = control or control_client(region)
        if args.action == "create":
            return create(client, region, out, wait)
        if args.action == "status":
            return status(client, region, out)
        return teardown(client, region, out, yes=args.yes)
    except Exception as exc:  # noqa: BLE001 -- the code and a sentence, never the text
        print(f"agentcore_memory: {error_words(exc)}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
