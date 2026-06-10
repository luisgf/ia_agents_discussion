from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class DebateMessage(BaseModel):
    role: str
    content: str


class ToolCallEntry(TypedDict):
    """Record of a single tool invocation made by an agent."""

    agent: str       # node name, e.g. "diagnostic_agent"
    tool_name: str   # e.g. "run_ssh_command"
    args: dict       # arguments passed to the tool
    result: str      # truncated output / error message
    error: bool      # True when the tool raised an exception


def _append_list(current: list | None, new: list | None) -> list:
    return (current or []) + (new or [])


class ModeratorDecision(BaseModel):
    status: Literal[
        "continue",
        "final_diagnosis",
        "needs_more_data",
        "propose_fix",
        "structured_uncertainty",
    ] = Field(description="Decision about the next workflow step.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the leading hypothesis.")
    leading_hypothesis: str = Field(description="Most likely technical cause or empty if unknown.")
    evidence: list[str] = Field(default_factory=list, description="Concrete supporting observations.")
    missing_evidence: list[str] = Field(default_factory=list, description="Data required to move forward safely.")
    rejected_hypotheses: list[str] = Field(default_factory=list, description="Alternatives considered and rejected.")
    next_step: str = Field(description="Cheapest safe diagnostic or remediation step.")
    recommended_fix: str | None = Field(default=None, description="Minimal reversible fix, if justified.")
    risk_level: Literal["low", "medium", "high"] = Field(description="Risk of the proposed next step or fix.")
    validation: list[str] = Field(default_factory=list, description="How to verify the diagnosis or fix.")
    stop_reason: str | None = Field(default=None, description="Reason to stop, if not continuing.")


def append_messages(
    current: list[DebateMessage] | None,
    new: list[DebateMessage] | None,
) -> list[DebateMessage]:
    return (current or []) + (new or [])


class DebateState(TypedDict):
    topic: str
    context: str
    round: int
    max_rounds: int
    confidence_threshold: float
    diagnostic_response: str
    skeptic_response: str
    diagnostic_rebuttal: str
    moderator_decision: ModeratorDecision | None
    history: Annotated[list[DebateMessage], append_messages]
    tool_calls_log: Annotated[list[ToolCallEntry], _append_list]
    final_result: str | None
    # Per-run model selections (resolved from form or settings defaults)
    diagnostic_model: str
    skeptic_model: str
    moderator_model: str
