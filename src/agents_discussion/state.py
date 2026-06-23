# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Pydantic requires typing_extensions.TypedDict (not typing.TypedDict) on Python < 3.12
# when a TypedDict is referenced from a model (e.g. ModeratorDecision.flow_directive).
from typing_extensions import TypedDict


class DebateMessage(BaseModel):
    role: str
    content: str


class ToolCallEntry(TypedDict):
    """Record of a single tool invocation made by an agent."""

    agent: str  # node name, e.g. "diagnostic_agent"
    tool_name: str  # e.g. "run_ssh_command"
    args: dict  # arguments passed to the tool
    result: str  # truncated output / error message
    error: bool  # True when the tool raised an exception
    approval: str  # auto | approved | rejected | timeout | cached


class HypothesisTransition(BaseModel):
    """State change of a hypothesis at a given round. Serialize with by_alias=True (``from`` is reserved)."""

    model_config = ConfigDict(populate_by_name=True)

    round: int = Field(description="Round in which the transition happened.")
    from_state: str | None = Field(default=None, alias="from", description="Previous state; None on creation.")
    to_state: str = Field(alias="to", description="New state.")
    agent: str = Field(description="Agent/node that caused the transition.")
    note: str = Field(default="", description="Short rationale for the transition.")


class Hypothesis(BaseModel):
    """Structured hypothesis tracked across debate rounds."""

    id: str = Field(description="Unique short id, e.g. H-1, H-2.")
    text: str = Field(description="Text of the hypothesis.")
    state: Literal["active", "rejected", "confirmed"] = Field(
        default="active", description="Current state of the hypothesis."
    )
    proposer: str = Field(description="Agent/node that proposed this hypothesis.")
    round: int = Field(description="Round in which this hypothesis was first proposed.")
    # No ge/le validation: the value is clamped to [0, 1] at extraction time so a
    # malformed LLM estimate never raises mid-run.
    probability: float | None = Field(
        default=None, description="Estimated probability 0-1 from the agents; None if not stated."
    )
    supporting_evidence: list[str] = Field(default_factory=list)
    rejected_reason: str | None = Field(default=None)
    transitions: list[HypothesisTransition] = Field(default_factory=list)


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


def _merge_hypotheses(
    current: list[Hypothesis] | None,
    new: list[Hypothesis] | None,
) -> list[Hypothesis]:
    """Reducer that merges hypotheses by id, keeping first-seen order.

    For an existing id: creation round and proposer are preserved; state, text and
    rejected_reason take the incoming values; evidence is unioned; transitions are
    concatenated with dedup, dropping incoming creation transitions (from_state None)
    so a re-emitted hypothesis does not duplicate its birth record.
    """
    merged: dict[str, Hypothesis] = {}
    for h in current or []:
        merged[h.id] = h.model_copy(deep=True)
    for h in new or []:
        existing = merged.get(h.id)
        if existing is None:
            merged[h.id] = h.model_copy(deep=True)
            continue
        if h.text.strip():
            existing.text = h.text
        existing.state = h.state
        if h.probability is not None:
            existing.probability = h.probability
        existing.rejected_reason = h.rejected_reason if h.rejected_reason is not None else existing.rejected_reason
        for ev in h.supporting_evidence:
            if ev not in existing.supporting_evidence:
                existing.supporting_evidence.append(ev)
        seen = {(t.round, t.to_state, t.agent) for t in existing.transitions}
        for t in h.transitions:
            if t.from_state is None:
                continue
            key = (t.round, t.to_state, t.agent)
            if key not in seen:
                existing.transitions.append(t.model_copy())
                seen.add(key)
    return list(merged.values())


def _merge_usage(
    current: dict[str, dict[str, int]] | None,
    new: dict[str, dict[str, int]] | None,
) -> dict[str, dict[str, int]]:
    """Reducer that merges token-usage dicts by summing counts per node."""
    result: dict[str, dict[str, int]] = dict(current or {})
    for key, val in (new or {}).items():
        if key in result:
            result[key] = {
                "input_tokens": (result[key].get("input_tokens", 0) or 0) + (val.get("input_tokens", 0) or 0),
                "output_tokens": (result[key].get("output_tokens", 0) or 0) + (val.get("output_tokens", 0) or 0),
                "total_tokens": (result[key].get("total_tokens", 0) or 0) + (val.get("total_tokens", 0) or 0),
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
        default=None, description="If set, controls which agents execute in the next round."
    )


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
    hypotheses: Annotated[list[Hypothesis], _merge_hypotheses]
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
