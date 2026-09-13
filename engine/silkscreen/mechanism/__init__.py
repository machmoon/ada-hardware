"""Mechanisms: jointed printed assemblies (a desktop robot arm), not just a box.

Engineered like :mod:`silkscreen.enclosure` (docs/ai-cad-plan.md v2): the
model proposes MECHANISM-SPEC v1 JSON only (:mod:`.ir`), every mechanical
number comes from a sourced table (:mod:`.rules`), :mod:`.layout` places the
features by arithmetic, :mod:`.kinematics` moves and loads the chain,
:mod:`.cad` builds real B-rep with build123d, and :mod:`.kernel` is the
acceptance gate -- frozen clauses, each with a signed margin.

Importing this package never imports build123d; building does.
"""

from .errors import MechanismBuildError, MechanismError, MechanismValidationError
from .ir import MECHANISM_SPEC_VERSION, Joint, MechanismSpec, parse_mechanism_spec

__all__ = [
    "MECHANISM_SPEC_VERSION",
    "Joint",
    "MechanismSpec",
    "parse_mechanism_spec",
    "MechanismError",
    "MechanismValidationError",
    "MechanismBuildError",
]
