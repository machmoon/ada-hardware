"""Tool failures go back to the model as guidance, bounded per tool.

Google ADK's ``ReflectAndRetryToolPlugin`` (``src/google/adk/plugins/
reflect_retry_tool_plugin.py``): a failing tool does not end the run, it
answers with a structured ``ToolFailureResponse`` carrying the error, the
retry count and guidance, and a per-tool counter that resets on success
stops the loop after ``max_retries``. Copied here as a plain object rather
than a plugin because the counter also feeds the receipt: "the model needed
three tries to satisfy ERC" is a fact about the run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

__all__ = ["ToolFailureResponse", "ReflectAndRetry", "REFLECT_RESPONSE_TYPE"]

REFLECT_RESPONSE_TYPE = "reflect_and_retry"


@dataclass(frozen=True)
class ToolFailureResponse:
    response_type: str
    error_type: str
    error_details: str
    retry_count: int
    reflection_guidance: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ReflectAndRetry:
    def __init__(self, max_retries: int = 3) -> None:
        self.max_retries = max_retries
        self.failures: dict[str, int] = {}

    def on_success(self, tool: str) -> None:
        self.failures.pop(tool, None)

    def on_failure(self, tool: str, exc: BaseException) -> ToolFailureResponse | None:
        """Guidance for the model, or None once ``max_retries`` is spent."""
        count = self.failures.get(tool, 0) + 1
        self.failures[tool] = count
        if count > self.max_retries:
            return None
        return ToolFailureResponse(
            response_type=REFLECT_RESPONSE_TYPE,
            error_type=type(exc).__name__,
            error_details=str(exc)[:800],
            retry_count=count,
            reflection_guidance=(
                f"The tool {tool!r} failed (attempt {count} of {self.max_retries}). "
                f"Read the error, correct the arguments or the artifact it checks, "
                f"and call it again. Do not claim it succeeded."
            ),
        )
