"""The first agent on the loop: propose a circuit that the verifiers accept.

The model's final answer is the circuit JSON the proposer already asks for
(``agents/propose.PROPOSE_PROMPT``, marker included so a
:class:`ScriptedToolModel` can key on it). The runner stores it in the
context and runs the required verifiers over it: ``validate_circuit`` (the
IR's own validation plus the package, part-number, library and signal rules
``propose_circuit`` runs, as one verdict with a clause per error),
``electrical_completeness`` and, when ``kicad-cli`` is installed and
``SILKSCREEN_ERC_IN_LOOP`` is not ``0``, ``erc``. A red verdict is the next
user turn; an ``unverified`` one ends the run saying why.

Not on either driver's stage list yet: ``agents/stages.py`` still calls
``propose_circuit``, which now runs the same verifiers inside its own
batched repair loop. This module exists so the loop can be driven and
measured on its own (``engine/tests/test_harness.py``), and the stage
switch is a one-line change once the scoreboard says the loop earns it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ...netlist import CircuitSpec, ValidationError, parse_circuit_spec
from ...verify import Clause, Verdict, electrical_completeness, erc_from_spec
from ...verify.kicad import kicad_cli_path
from ..propose import ERC_IN_LOOP_ENV, PROPOSE_PROMPT, part_number_errors
from .loop import Agent, Budget, Runner, RunResult
from .model import ToolModel
from .tools import function_tool, verifier_tool

__all__ = ["DESIGN_MARKER", "design_agent", "run_design", "validate_circuit"]

DESIGN_MARKER = "DESIGN-CIRCUIT v1"


def validate_circuit(context: dict[str, Any]) -> Verdict:
    """The IR's validation and the proposer's rule set, as one verdict.

    Reads ``context["proposal"]`` (the model's JSON) and, when it is a valid
    circuit, leaves ``context["spec"]`` for the verifiers after it.
    """
    proposal = context.get("proposal")
    if proposal is None:
        return Verdict.unverified("validate_circuit", "no proposal in the context")
    try:
        spec = parse_circuit_spec(
            proposal if isinstance(proposal, str) else json.dumps(proposal)
        )
    except ValidationError as exc:
        context.pop("spec", None)
        return Verdict(
            "validate_circuit",
            tuple(
                Clause(f"validate:{i}", False, str(e)) for i, e in enumerate(exc.errors)
            ),
        )
    except Exception as exc:  # unparseable JSON, the wrong shape entirely
        context.pop("spec", None)
        return Verdict("validate_circuit", (Clause("validate:parse", False, str(exc)),))
    from ...board import package_errors, tie_package_pins
    from ...signals import signal_errors

    spec, _tied = tie_package_pins(spec)
    errors = package_errors(spec) + part_number_errors(spec) + signal_errors(spec)
    context["spec"] = spec
    if errors:
        return Verdict(
            "validate_circuit",
            tuple(Clause(f"rule:{i}", False, e) for i, e in enumerate(errors)),
        )
    return Verdict(
        "validate_circuit", (Clause("validate", True, "the circuit is well-formed"),)
    )


def _completeness(context: dict[str, Any]) -> Verdict:
    spec: CircuitSpec | None = context.get("spec")
    if spec is None:
        return Verdict.unverified(
            "electrical_completeness", "no valid circuit to check"
        )
    return electrical_completeness(spec)


def _erc(context: dict[str, Any]) -> Verdict:
    spec: CircuitSpec | None = context.get("spec")
    if spec is None:
        return Verdict.unverified("erc", "no valid circuit to draw")
    return erc_from_spec(spec)


def check_circuit(context: dict[str, Any], circuit_json: str) -> dict[str, Any]:
    """Run the validation and electrical checks on a draft before answering."""
    context["proposal"] = circuit_json
    validation = validate_circuit(context)
    out: dict[str, Any] = {"validate_circuit": validation.as_dict()}
    if validation.ok:
        out["electrical_completeness"] = _completeness(context).as_dict()
    return out


def erc_enabled() -> bool:
    return (
        os.getenv(ERC_IN_LOOP_ENV, "1").strip() != "0" and kicad_cli_path() is not None
    )


def design_agent(*, erc: bool | None = None, extra_instructions: str = "") -> Agent:
    """The proposer as an agent: the prompt it already uses, plus the gate."""
    use_erc = erc_enabled() if erc is None else erc
    tools = [
        function_tool(check_circuit, name="check_circuit"),
        verifier_tool(
            "validate_circuit",
            "Check the circuit JSON you are about to answer with.",
            validate_circuit,
        ),
        verifier_tool(
            "electrical_completeness",
            "Ground and supply rules over the validated circuit.",
            _completeness,
        ),
    ]
    required = ["validate_circuit", "electrical_completeness"]
    if use_erc:
        tools.append(
            verifier_tool(
                "erc", "KiCad's ERC on the schematic this circuit draws.", _erc
            )
        )
        required.append("erc")
    instructions = (
        f"{DESIGN_MARKER}\n{PROPOSE_PROMPT}\n{extra_instructions}\n"
        "Answer with the circuit JSON and nothing else. Every check named in "
        "your tools is run on your answer; a failed check comes back as a list "
        "of items to fix."
    )
    return Agent(
        name="design",
        instructions=instructions,
        tools=tuple(tools),
        required_verifiers=tuple(required),
        output_type="json",
        artifact_key="proposal",
        max_output_tokens=65536,
    )


def run_design(
    intent: str,
    *,
    model: ToolModel,
    budget: Budget | None = None,
    on_event=None,
    erc: bool | None = None,
    extra_instructions: str = "",
) -> RunResult:
    agent = design_agent(erc=erc, extra_instructions=extra_instructions)
    return Runner().run(
        agent,
        f"What to build:\n{intent}",
        model=model,
        budget=budget,
        on_event=on_event,
    )
