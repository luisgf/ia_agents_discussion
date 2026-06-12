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
    approval: str    # auto | approved | rejected | timeout


class Hypothesis(BaseModel):
    """Structured hypothesis tracked across debate rounds."""

    id: str = Field(description="Unique short id, e.g. H-1, H-2.")
    text: str = Field(description="Text of the hypothesis.")
    state: Literal["active", "rejected", "confirmed"] = Field(
        default="active", description="Current state of the hypothesis.")
    proposer: str = Field(description="Agent/node that proposed this hypothesis.")
    round: int = Field(description="Round in which this hypothesis was proposed or last updated.")
    supporting_evidence: list[str] = Field(default_factory=list)
    rejected_reason: str | None = Field(default=None)


class FlowDirective(TypedDict):
    """Moderator instruction for the next round's flow."""

    skip_skeptic: bool
    skip_rebuttal: bool
    rationale: str


class DebateRound(TypedDict):
    """Structured summary of a single debate round."""

    round: int
    diagnostic: str
    skeptic: str
    rebuttal: str
    moderator: dict  # serialized ModeratorDecision


def _append_list(current: list | None, new: list | None) -> list:
    return (current or []) + (new or [])


def _merge_usage(
    current: dict[str, dict[str, int]] | None,
    new: dict[str, dict[str, int]] | None,
) -> dict[str, dict[str, int]]:
    """Reducer that merges token-usage dicts by summing counts per node."""
    result: dict[str, dict[str, int]] = dict(current or {})
    for key, val in (new or {}).items():
        if key in result:
            result[key] = {
                "input_tokens":  (result[key].get("input_tokens",  0) or 0) + (val.get("input_tokens",  0) or 0),
                "output_tokens": (result[key].get("output_tokens", 0) or 0) + (val.get("output_tokens", 0) or 0),
                "total_tokens":  (result[key].get("total_tokens",  0) or 0) + (val.get("total_tokens",  0) or 0),
            }
        else:
            result[key] = {k: v or 0 for k, v in val.items()}
    return result


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
    flow_directive: FlowDirective | None = Field(
        default=None,
        description="If set, controls which agents execute in the next round.")


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
    early_out_threshold: float
    diagnostic_response: str
    skeptic_response: str
    diagnostic_rebuttal: str
    moderator_decision: ModeratorDecision | None
    history: Annotated[list[DebateMessage], append_messages]
    tool_calls_log: Annotated[list[ToolCallEntry], _append_list]
    final_result: str | None
    # Structured hypotheses tracked across rounds
    hypotheses: Annotated[list[Hypothesis], _append_list]
    # Compressed history summary for rounds > 2
    history_summary: str
    # Per-round structured log (useful for reports and summaries)
    round_log: Annotated[list[DebateRound], _append_list]
    # Early-out signal from diagnostic agent
    early_out_recommended: bool
    early_out_confidence: float
    early_out_rationale: str
    # Per-run model selections (resolved from form or settings defaults)
    diagnostic_model: str
    skeptic_model: str
    moderator_model: str
    summary_model: str
    # Per-run thinking level (none|low|medium|high) per agent
    diagnostic_reasoning_effort: str
    skeptic_reasoning_effort: str
    moderator_reasoning_effort: str
    # Web-run plumbing: run_id links graph nodes to the RunControl registry
    # (tool approval, human-in-the-loop, cancellation). Empty for CLI runs.
    run_id: str
    # Prompt template selection (see prompt_store.py)
    template: str
    language: str
    # Compression enabled flag
    compress_history: bool
    # Token usage accumulated across all LLM calls (agent_node → counts)
    token_usage: Annotated[dict[str, dict[str, int]], _merge_usage]
