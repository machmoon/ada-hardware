"""Error taxonomy for the mechanism layer.

The enclosure's rule (``enclosure/errors.py``): every failure path raises one
of these, because an agent handed an empty result concludes the arm works.
"""

from __future__ import annotations

__all__ = [
    "MechanismError",
    "MechanismValidationError",
    "MechanismBuildError",
]


class MechanismError(Exception):
    """Base class for every mechanism failure."""


class MechanismValidationError(MechanismError):
    """A proposed spec is invalid; ``errors`` is the whole batch, one message
    per problem, for a single repair prompt (``netlist.ValidationError``)."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in mechanism spec:\n  - "
            + "\n  - ".join(errors)
        )


class MechanismBuildError(MechanismError):
    """The spec validated but cannot be built as stated.

    ``failure_class`` is a stable word (``LINK_TOO_SHORT``, ``BOOLEAN_FAILED``,
    ``INVALID_SHAPE``) and ``str()`` reads ``"<failure_class>: <detail>"``,
    the :class:`~silkscreen.enclosure.errors.KernelError` shape, so the repair
    prompt names the class.
    """

    def __init__(self, failure_class: str, detail: str):
        self.failure_class = failure_class
        self.detail = detail
        super().__init__(f"{failure_class}: {detail}")
