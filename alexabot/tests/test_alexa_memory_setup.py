"""``scripts/aws/agentcore_memory.py`` against botocore's ``Stubber``: create is
idempotent and exact about the name, teardown needs ``--yes``, and nothing it
prints holds a key, an ARN or an account number. Offline.

Loaded with ``spec_from_file_location`` (the ``engine/tests`` precedent for a
script). Skips without botocore through a ``skipif`` mark, the check_docs rule.
"""

import datetime
import importlib.util
import io
import re
from pathlib import Path

import pytest

from alexabot import memory

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("botocore") is None,
    reason='botocore is not installed: pip install -e ".[alexa]"')

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aws" / "agentcore_memory.py"
NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)
OURS = "AdaDesignPreferences-a1B2c3D4e5"
LEAKS = re.compile(r"\b\d{12}\b|arn:aws|AKIA|secret", re.IGNORECASE)


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("agentcore_memory_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def control(loopback_only):
    import botocore.session
    from botocore.stub import Stubber

    c = botocore.session.get_session().create_client(
        "bedrock-agentcore-control", region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing")
    with Stubber(c) as stub:
        yield c, stub
        stub.assert_no_pending_responses()


def summary(mid, status="ACTIVE"):
    return {"arn": f"arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/{mid}",
            "id": mid, "status": status, "createdAt": NOW, "updatedAt": NOW}


def full(mid, status="ACTIVE"):
    return {"memory": {
        **summary(mid, status), "name": memory.MEMORY_NAME, "eventExpiryDuration": 7,
        "strategies": [{"strategyId": "DesignPreferences-x",
                        "name": "DesignPreferences",
                        "type": "USER_PREFERENCE",
                        "namespaces": [memory.NAMESPACE_TEMPLATE],
                        "namespaceTemplates": [memory.NAMESPACE_TEMPLATE]}]}}


def run(script, c, *argv):
    out = io.StringIO()
    code = script.main(list(argv), control=c, out=out,
                       wait={"Delay": 0, "MaxAttempts": 2})
    text = out.getvalue()
    assert not LEAKS.search(text), text
    return code, text


def test_create_when_absent_creates_waits_and_prints_the_export(script, control):
    from botocore.stub import ANY

    c, stub = control
    stub.add_response("list_memories", {"memories": [
        summary("AdaDesignPreferencesOld-a1B2c3D4e5")]})
    stub.add_response("create_memory", full(OURS, "CREATING"), {
        "clientToken": ANY, "name": memory.MEMORY_NAME,
        "description": memory.MEMORY_DESCRIPTION, "eventExpiryDuration": 7,
        "memoryStrategies": [memory.STRATEGY]})
    stub.add_response("get_memory", full(OURS, "ACTIVE"), {"memoryId": OURS})
    code, text = run(script, c, "create")
    assert code == 0
    assert f"export ADA_AGENTCORE_MEMORY_ID={OURS}" in text
    assert "status:   ACTIVE" in text


def test_create_when_present_matches_the_exact_name_and_creates_nothing(script,
                                                                        control):
    c, stub = control
    stub.add_response("list_memories", {"memories": [summary(OURS)]})
    stub.add_response("get_memory", full(OURS), {"memoryId": OURS})
    code, text = run(script, c, "create")
    assert code == 0 and "already exists; nothing created" in text
    assert "problem:" not in text


def test_a_waiter_that_runs_out_exits_3_in_words(script, control):
    from botocore.stub import ANY

    c, stub = control
    stub.add_response("list_memories", {"memories": []})
    stub.add_response("create_memory", full(OURS, "CREATING"), {
        "clientToken": ANY, "name": memory.MEMORY_NAME,
        "description": memory.MEMORY_DESCRIPTION, "eventExpiryDuration": 7,
        "memoryStrategies": [memory.STRATEGY]})
    for _ in range(2):
        stub.add_response("get_memory", full(OURS, "CREATING"), {"memoryId": OURS})
    code, text = run(script, c, "create")
    assert code == 3 and "Still creating" in text and "status" in text


def test_teardown_needs_yes(script, control):
    from botocore.stub import ANY

    c, stub = control
    stub.add_response("list_memories", {"memories": [summary(OURS)]})
    code, text = run(script, c, "teardown")
    assert code == 1 and "--yes" in text
    stub.add_response("list_memories", {"memories": [summary(OURS)]})
    stub.add_response("delete_memory", {"memoryId": OURS, "status": "DELETING"},
                      {"memoryId": OURS, "clientToken": ANY})
    code, text = run(script, c, "teardown", "--yes")
    assert code == 0 and "DELETING" in text


def test_an_aws_error_is_its_code_and_a_sentence(script, control, capsys):
    c, stub = control
    stub.add_client_error("list_memories", "AccessDeniedException",
                          "User arn:aws:iam::123456789012:user/pat is not authorized")
    assert script.main(["status"], control=c, out=io.StringIO()) == 1
    err = capsys.readouterr().err
    assert "AccessDeniedException: this AWS identity may not manage" in err
    assert not LEAKS.search(err)
