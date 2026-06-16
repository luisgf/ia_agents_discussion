import json
import logging
import re
import time
import uuid

_log = logging.getLogger(__name__)

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agents_discussion.audit import audit_tool_call
from agents_discussion.config import get_settings
from agents_discussion.models import _normalize_effort, create_github_model, supports_reasoning
from agents_discussion.pricing import estimate_cost
from agents_discussion.prompt_store import PromptTemplate, get_template
from agents_discussion.prompts import (
    diagnostic_prompt,
    moderator_json_fallback_suffix,
    moderator_prompt,
    rebuttal_prompt,
    skeptic_prompt,
)
from agents_discussion.runtime import RunCancelled, ToolCache, get_control
from agents_discussion.state import (
    DebateMessage,
    DebateRound,
    DebateState,
    FlowDirective,
    Hypothesis,
    HypothesisTransition,
    ModeratorDecision,
    ToolCallEntry,
    _merge_hypotheses,
)
from agents_discussion.tools import get_tools


AGENT_EVENT_FIELDS = {
    "diagnostic_agent": ("diagnostic_response", "Diagnóstico Principal"),
    "skeptic_agent": ("skeptic_response", "Revisor Escéptico"),
    "diagnostic_rebuttal_agent": ("diagnostic_rebuttal", "Contrarréplica"),
}


def _chunk_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text"):
                parts.append(part.get("text", ""))
        return "".join(parts)
    return ""


def _message_content(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _chunk_text(content)
    return str(content)


def _template_for(state: DebateState) -> PromptTemplate:
    return get_template(state.get("template", ""), state.get("language", ""))


def _resolve_effort(state: DebateState, effort_key: str, model: str, agent_node: str, agent_role: str) -> str | None:
    """Normalize the requested thinking level for an agent."""
    effort = _normalize_effort(state.get(effort_key))
    if effort is None:
        return None
    if supports_reasoning(model):
        return effort
    control = get_control(state.get("run_id", ""))
    if control is not None and control.warn_once(f"effort:{agent_node}"):
        control.emit({
            "type": "reasoning_effort_ignored",
            "agent_node": agent_node,
            "agent_role": agent_role,
            "model": model,
            "requested_effort": effort,
        })
    return None


def _invoke_streaming(model, messages: list, control, agent_node: str, agent_role: str):
    """Invoke model with streaming to UI. Falls back to blocking invoke.

    Returns the aggregated response object; callers can read ``response.usage_metadata``
    to obtain token counts (``input_tokens``, ``output_tokens``, ``total_tokens``).
    ``stream_usage=True`` is passed so the final chunk carries usage data when the
    endpoint supports it; it is silently ignored on endpoints that do not.
    """
    if control is None:
        return model.invoke(messages)

    control.emit({
        "type": "agent_turn_started",
        "agent_node": agent_node,
        "agent_role": agent_role,
    })
    response = None
    try:
        for chunk in model.stream(messages, stream_usage=True):
            control.raise_if_cancelled()
            response = chunk if response is None else response + chunk
            delta = _chunk_text(chunk.content)
            if delta:
                control.emit({
                    "type": "agent_delta",
                    "agent_node": agent_node,
                    "agent_role": agent_role,
                    "delta": delta,
                })
    except RunCancelled:
        raise
    except Exception:
        if response is not None:
            raise
        return model.invoke(messages)
    if response is None:
        return model.invoke(messages)
    return response


# ── Helpers for structured parsing ──────────────────────────────────────────

_HYPOTHESIS_RE = re.compile(
    r"###\s*HYPOTHESIS-([A-Za-z0-9_-]+)"
    r"(?:\s*\[\s*P\s*=\s*([0-9]*\.?[0-9]+)\s*\])?"
    r"\s*\n\s*Text:\s*(.+?)(?=\n###|\n##|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_SNIPPET_P_RE = re.compile(r"\[\s*P\s*=\s*([0-9]*\.?[0-9]+)\s*\]", re.IGNORECASE)

_EARLY_OUT_RE = re.compile(
    r"EARLY_OUT_RECOMMENDED:\s*(true|yes|1)",
    re.IGNORECASE,
)

_EARLY_CONF_RE = re.compile(
    r"EARLY_OUT_CONFIDENCE:\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)

_EARLY_RATIONALE_RE = re.compile(
    r"EARLY_OUT_RATIONALE:\s*(.+?)(?=\n[A-Z_]+:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


_EVIDENCE_LINE_RE = re.compile(r"\[tool:[^\]]+\]")

_MAX_EVIDENCE_PER_HYPOTHESIS = 5
_MAX_EVIDENCE_CHARS = 250


def _split_hypothesis_block(block: str) -> tuple[str, list[str]]:
    """Split a captured hypothesis block into text and supporting evidence lines.

    The regex captures everything after ``Text:`` up to the next heading, so the
    block may include the agent's supporting observations. The hypothesis text is
    the first paragraph; lines citing tool outputs ([tool:<name>]) become evidence.
    """
    first_para, _, rest = block.partition("\n\n")
    text = " ".join(line.strip() for line in first_para.strip().splitlines() if line.strip())
    evidence: list[str] = []
    for line in rest.splitlines():
        line = line.strip().lstrip("-·* ").strip()
        if line and _EVIDENCE_LINE_RE.search(line) and line not in evidence:
            evidence.append(line[:_MAX_EVIDENCE_CHARS])
            if len(evidence) >= _MAX_EVIDENCE_PER_HYPOTHESIS:
                break
    return text, evidence


def _parse_probability(raw: str | None) -> float | None:
    """Clamp an LLM-emitted probability to [0, 1]; None when absent or unparsable."""
    if not raw:
        return None
    try:
        return min(max(float(raw), 0.0), 1.0)
    except ValueError:
        return None


def _creation_transition(proposer: str, round_number: int) -> HypothesisTransition:
    return HypothesisTransition(
        round=round_number, from_state=None, to_state="active",
        agent=proposer, note="Hipótesis propuesta",
    )


def _extract_hypotheses(text: str, proposer: str, round_number: int) -> list[Hypothesis]:
    """Extract structured hypotheses from agent text response."""
    hypotheses: list[Hypothesis] = []
    matches = list(_HYPOTHESIS_RE.finditer(text))
    # If no structured format found, fall back to extracting the first bold/heading line
    if not matches:
        # Try to find a leading hypothesis in the first paragraph.
        # Round-scoped id: a fallback id has no LLM references to preserve, and a
        # fixed "H-1" would falsely merge unrelated fallbacks across rounds.
        first_para = text.split("\n\n")[0] if text else ""
        if first_para:
            hypotheses.append(Hypothesis(
                id=f"R{round_number}-F1",
                text=first_para.strip(),
                state="active",
                proposer=proposer,
                round=round_number,
                transitions=[_creation_transition(proposer, round_number)],
            ))
        return hypotheses

    for match in matches:
        hyp_id = match.group(1).strip()
        hyp_text, evidence = _split_hypothesis_block(match.group(3))
        hypotheses.append(Hypothesis(
            id=hyp_id,
            text=hyp_text,
            state="active",
            proposer=proposer,
            round=round_number,
            probability=_parse_probability(match.group(2)),
            supporting_evidence=evidence,
            transitions=[_creation_transition(proposer, round_number)],
        ))
    return hypotheses


def _extract_early_out(text: str) -> dict[str, object]:
    """Extract early-out signal from diagnostic agent response."""
    rec_match = _EARLY_OUT_RE.search(text)
    conf_match = _EARLY_CONF_RE.search(text)
    rat_match = _EARLY_RATIONALE_RE.search(text)
    return {
        "recommended": bool(rec_match),
        "confidence": float(conf_match.group(1)) if conf_match else 0.0,
        "rationale": rat_match.group(1).strip() if rat_match else "",
    }


def _last_round_messages(history: list[DebateMessage], current_round: int) -> list[DebateMessage]:
    """Return messages from the current round only.

    Round N starts after the (N-1)th moderator decision in history.
    """
    if not history:
        return []
    mod_count = 0
    for i, msg in enumerate(history):
        if msg.role == "moderator":
            mod_count += 1
            if mod_count == current_round - 1:
                return history[i + 1:]
    # No previous moderator found → round 1 → entire history
    return history


# Roles written to history by the current round's agents (the rebuttal writes
# "diagnostic_rebuttal", not "diagnostic_rebuttal_agent").
_AGENT_HISTORY_ROLES = frozenset({"diagnostic_agent", "skeptic_agent", "diagnostic_rebuttal"})


def _history_before_current_round(history: list[DebateMessage], current_round: int) -> list[DebateMessage]:
    """History up to the end of the previous round, plus user comments of the current one.

    Used by agents whose prompt already includes the current round's responses
    explicitly (skeptic, rebuttal, moderator) so they are not duplicated.
    HITL user comments injected after the previous moderator are preserved.
    """
    if not history:
        return []
    mod_count = 0
    for i, msg in enumerate(history):
        if msg.role == "moderator":
            mod_count += 1
            if mod_count == current_round - 1:
                return history[:i + 1] + [m for m in history[i + 1:] if m.role == "user"]
    # Round 1 (or resume preamble without moderator messages): drop only the
    # current round's agent messages.
    return [m for m in history if m.role not in _AGENT_HISTORY_ROLES]


def _messages_after_last_moderator(history: list[DebateMessage]) -> list[DebateMessage]:
    """Messages of the round in progress (everything after the last moderator decision)."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].role == "moderator":
            return history[i + 1:]
    return list(history)


# ── ReAct loop (unchanged core) ─────────────────────────────────────────────

def _run_with_tools(
    model_factory,
    agent_node: str,
    system_prompt: str,
    user_message: str,
    run_id: str = "",
    round_number: int = 0,
) -> tuple[str, list[ToolCallEntry], dict[str, int]]:
    """Run a model in a ReAct loop: LLM → tool call(s) → LLM → … → final text.

    Returns:
        Tuple of (final_content, tool_invocation_log, token_usage_dict).
        ``token_usage_dict`` has keys ``input_tokens``, ``output_tokens``, ``total_tokens``.
    """
    settings = get_settings()
    control = get_control(run_id)
    # CLI runs (no control) still get an ephemeral cache → intra-loop dedup.
    tool_cache = control.tool_cache if control is not None else ToolCache()
    tools = get_tools() if settings.tools_enabled else []
    model = model_factory()
    if tools:
        model = model.bind_tools(tools)
    tool_map = {t.name: t for t in tools}

    messages: list = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ]
    tool_log: list[ToolCallEntry] = []
    call_count = 0
    consecutive_errors = 0
    agent_role = AGENT_EVENT_FIELDS.get(agent_node, ("", agent_node))[1]

    # Accumulate token usage across all LLM calls in this ReAct loop
    total_input = 0
    total_output = 0
    total_tokens = 0

    while call_count <= settings.max_tool_calls_per_agent:
        if control is not None:
            control.raise_if_cancelled()

        response = _invoke_streaming(model, messages, control, agent_node, agent_role)
        messages.append(response)

        # Accumulate usage from this LLM call
        usage_meta = getattr(response, "usage_metadata", None) or {}
        total_input  += usage_meta.get("input_tokens",  0) or 0
        total_output += usage_meta.get("output_tokens", 0) or 0
        total_tokens += usage_meta.get("total_tokens",  0) or 0

        raw_calls = getattr(response, "tool_calls", None) or []
        if not raw_calls:
            break

        reasoning_text = _message_content(response)
        if control is not None and reasoning_text.strip():
            control.emit({
                "type": "agent_reasoning",
                "agent_node": agent_node,
                "agent_role": agent_role,
                "content": reasoning_text,
            })

        batch_had_error = False
        for tc in raw_calls:
            if call_count >= settings.max_tool_calls_per_agent:
                messages.append(
                    ToolMessage(content="Tool call limit reached.", tool_call_id=tc["id"])
                )
                continue
            call_count += 1

            tool_name: str = tc["name"]
            tool_args: dict = tc["args"]
            call_id: str = tc["id"]
            exec_id = uuid.uuid4().hex[:12]
            duration_ms: int | None = None

            def _execute(tool_fn) -> tuple[str, bool]:
                nonlocal duration_ms
                if control is not None:
                    control.emit({
                        "type": "tool_call_started",
                        "call_id": exec_id,
                        "agent_node": agent_node,
                        "agent_role": agent_role,
                        "tool_name": tool_name,
                        "args": tool_args,
                    })
                started = time.monotonic()
                try:
                    return str(tool_fn.invoke(tool_args)), False
                except Exception as exc:
                    return f"Tool error: {exc}", True
                finally:
                    duration_ms = int((time.monotonic() - started) * 1000)

            tool_fn = tool_map.get(tool_name)
            approval = "auto"
            cache_hit = tool_cache.get(tool_name, tool_args, round_number) if tool_fn is not None else None
            if tool_fn is None:
                result = f"Unknown tool: {tool_name}"
                error = True
            elif cache_hit is not None:
                # Same command already approved and executed this round: serve the
                # stored output without re-executing or re-asking for approval.
                result = (
                    f"[cached: ya ejecutado por {cache_hit.agent_role} en ronda {cache_hit.round}]\n"
                    f"{cache_hit.result}"
                )
                error = False
                approval = "cached"
                duration_ms = 0
            elif control is not None and control.needs_approval(tool_name):
                approved, approval = control.request_approval(tool_name, tool_args, agent_role)
                if approved:
                    result, error = _execute(tool_fn)
                else:
                    result = (
                        "El operador no aprobó esta llamada "
                        f"({'timeout' if approval == 'timeout' else 'rechazada'}). "
                        "No la repitas; usa otra herramienta o continúa con la "
                        "información disponible."
                    )
                    error = False
            else:
                result, error = _execute(tool_fn)

            if not error and approval in ("auto", "approved"):
                tool_cache.put(tool_name, tool_args, result, agent_node, agent_role, round_number)

            audit_tool_call(run_id, agent_node, tool_name, tool_args, result, error, approval)
            entry = ToolCallEntry(
                agent=agent_node,
                tool_name=tool_name,
                args=tool_args,
                result=result,
                error=error,
                approval=approval,
            )
            tool_log.append(entry)
            if control is not None:
                control.emit({
                    "type": "tool_call",
                    "call_id": exec_id,
                    "agent_node": agent_node,
                    "agent_role": agent_role,
                    "tool_name": tool_name,
                    "args": tool_args,
                    "result": result,
                    "error": error,
                    "approval": approval,
                    "cached": approval == "cached",
                    "duration_ms": duration_ms,
                })
            messages.append(ToolMessage(content=result, tool_call_id=call_id))
            if error:
                batch_had_error = True

        if batch_had_error:
            consecutive_errors += 1
            messages.append(
                HumanMessage(
                    content=(
                        "Una o más herramientas devolvieron error. "
                        "Analiza los mensajes, corrige los parámetros si es posible "
                        "e intenta de nuevo con una estrategia diferente. "
                        "Si el error no es recuperable, usa otra herramienta o "
                        "documenta la limitación en tu respuesta final."
                    )
                )
            )
            if consecutive_errors >= settings.max_consecutive_errors:
                break
        else:
            consecutive_errors = 0

    content = _message_content(response)
    if not content.strip():
        # Algunos modelos razonadores (p.ej. Kimi) cierran una cadena larga de tools
        # con un mensaje visible vacío. Pedir la respuesta final una vez antes de rendirse.
        _log.warning("%s returned empty final content after %d tool calls; nudging once", agent_node, call_count)
        messages.append(
            HumanMessage(
                content=(
                    "No has entregado tu respuesta final. Escribe AHORA tu informe final "
                    "completo según el formato pedido en el mensaje inicial, sin llamar "
                    "a más herramientas."
                )
            )
        )
        response = _invoke_streaming(model, messages, control, agent_node, agent_role)
        usage_meta = getattr(response, "usage_metadata", None) or {}
        total_input  += usage_meta.get("input_tokens",  0) or 0
        total_output += usage_meta.get("output_tokens", 0) or 0
        total_tokens += usage_meta.get("total_tokens",  0) or 0
        content = _message_content(response)

    if not content.strip():
        content = f"(El agente no entregó respuesta final tras {call_count} llamadas a herramientas.)"

    node_usage = {
        "input_tokens":  total_input,
        "output_tokens": total_output,
        "total_tokens":  total_tokens if total_tokens else (total_input + total_output),
    }
    return content, tool_log, node_usage


# ── Agent nodes ──────────────────────────────────────────────────────────────


def _history_mode(state: DebateState) -> tuple[str, str]:
    """Determine history mode and summary for prompts (compressed from round 2 on)."""
    summary = state.get("history_summary", "")
    current_round = state.get("round", 1)
    compress = state.get("compress_history", True)

    if not compress or current_round < 2 or not summary:
        return "", "full"
    return summary, "compressed"


def _prompt_history(
    state: DebateState,
    *,
    exclude_current_round: bool,
) -> tuple[list[DebateMessage], str, str]:
    """Return (history_for_prompt, summary, mode) for an agent prompt.

    In compressed mode the summary replaces finished rounds, so only the
    messages after the last moderator decision are passed (the in-progress
    round). With exclude_current_round=True (skeptic/rebuttal/moderator,
    whose prompts already carry the current responses explicitly) only the
    user's HITL comments survive from that tail.
    """
    history = state.get("history", [])
    summary, mode = _history_mode(state)
    if mode == "compressed":
        tail = _messages_after_last_moderator(history)
        if exclude_current_round:
            tail = [m for m in tail if m.role == "user"]
        else:
            tail = [m for m in tail if m.role not in _AGENT_HISTORY_ROLES]
        return tail, summary, mode
    if exclude_current_round:
        return _history_before_current_round(history, state.get("round", 1)), summary, mode
    return history, summary, mode


def diagnostic_agent(state: DebateState) -> dict[str, object]:
    template = _template_for(state)
    effort = _resolve_effort(
        state, "diagnostic_reasoning_effort", state["diagnostic_model"],
        "diagnostic_agent", "Diagnóstico Principal",
    )
    prompt_history, history_summary, mode = _prompt_history(state, exclude_current_round=False)
    content, tool_log, usage = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2, reasoning_effort=effort),
        "diagnostic_agent",
        template.diagnostic_system,
        diagnostic_prompt(
            state["topic"], state["context"], state["round"], prompt_history,
            hypotheses=state.get("hypotheses", []),
            language=state.get("language", "es"),
            history_summary=history_summary,
            mode=mode,
        ),
        run_id=state.get("run_id", ""),
        round_number=state["round"],
    )

    # Extract structured hypotheses and early-out signal
    hypotheses = _extract_hypotheses(content, "diagnostic_agent", state["round"])
    early = _extract_early_out(content)

    return {
        "diagnostic_response": content,
        "history": [DebateMessage(role="diagnostic_agent", content=content)],
        "tool_calls_log": tool_log,
        "hypotheses": hypotheses,
        "early_out_recommended": early["recommended"],
        "early_out_confidence": early["confidence"],
        "early_out_rationale": early["rationale"],
        "token_usage": {"diagnostic_agent": usage},
    }


def _should_skip(state: DebateState, agent_node: str) -> bool:
    """Check if the moderator requested skipping this agent in the next round."""
    # The flow directive from the PREVIOUS round's moderator decision controls THIS round
    # We look at the most recent moderator decision in history
    history = state.get("history", [])
    for msg in reversed(history):
        if msg.role == "moderator":
            try:
                decision = json.loads(msg.content)
                fd = decision.get("flow_directive")
                if fd:
                    if agent_node == "skeptic_agent" and fd.get("skip_skeptic"):
                        return True
                    if agent_node == "diagnostic_rebuttal_agent" and fd.get("skip_rebuttal"):
                        return True
            except (json.JSONDecodeError, AttributeError):
                continue
    return False


def skeptic_agent(state: DebateState) -> dict[str, object]:
    if _should_skip(state, "skeptic_agent"):
        return {
            "skeptic_response": "(Fase escéptica omitida por decisión del moderador.)",
            "history": [DebateMessage(role="skeptic_agent", content="(omitido)")],
            "tool_calls_log": [],
        }

    template = _template_for(state)
    effort = _resolve_effort(
        state, "skeptic_reasoning_effort", state["skeptic_model"],
        "skeptic_agent", "Revisor Escéptico",
    )
    prompt_history, history_summary, mode = _prompt_history(state, exclude_current_round=True)
    content, tool_log, usage = _run_with_tools(
        lambda: create_github_model(state["skeptic_model"], temperature=0.1, reasoning_effort=effort),
        "skeptic_agent",
        template.skeptic_system,
        skeptic_prompt(
            state["topic"], state["context"], state["diagnostic_response"],
            state.get("hypotheses", []), prompt_history,
            language=state.get("language", "es"),
            history_summary=history_summary,
            mode=mode,
        ),
        run_id=state.get("run_id", ""),
        round_number=state["round"],
    )

    # Update hypothesis states based on skeptic feedback
    # Simple heuristic: if skeptic mentions rejecting a hypothesis by ID, update it.
    # Only modified hypotheses are returned; the merge-by-id reducer folds them in.
    updated_hypotheses: list[Hypothesis] = []
    for h in state.get("hypotheses", []):
        if f"[hypothesis:{h.id}]" not in content and f"HYPOTHESIS-{h.id}" not in content:
            continue
        idx = content.find(f"[hypothesis:{h.id}]")
        if idx == -1:
            idx = content.find(f"HYPOTHESIS-{h.id}")
        snippet = content[idx:idx + 500] if idx >= 0 else ""
        new_state: str | None = None
        rejected_reason: str | None = None
        if re.search(r"\brejected\b|\brechazada?\b|\binválida?\b", snippet, re.IGNORECASE):
            new_state = "rejected"
            reason_match = re.search(r"[Rr]eason:\s*(.+?)(?=\n\n|\Z)", snippet, re.DOTALL)
            rejected_reason = reason_match.group(1).strip() if reason_match else "Rechazada por el escéptico."
        elif re.search(r"\baccepted\b|\baceptada?\b|\bconfirmada?\b", snippet, re.IGNORECASE):
            new_state = "confirmed"
        prob_match = _SNIPPET_P_RE.search(snippet)
        new_prob = _parse_probability(prob_match.group(1)) if prob_match else None
        state_changed = new_state is not None and new_state != h.state
        if not state_changed and new_prob is None:
            continue
        new_h = h.model_copy(deep=True)
        if new_prob is not None:
            new_h.probability = new_prob
        if state_changed:
            new_h.state = new_state
            new_h.rejected_reason = rejected_reason
            new_h.transitions = new_h.transitions + [HypothesisTransition(
                round=state["round"], from_state=h.state, to_state=new_state,
                agent="skeptic_agent",
                note=rejected_reason or "Confirmada por el escéptico.",
            )]
        updated_hypotheses.append(new_h)

    return {
        "skeptic_response": content,
        "history": [DebateMessage(role="skeptic_agent", content=content)],
        "tool_calls_log": tool_log,
        "hypotheses": updated_hypotheses,
        "token_usage": {"skeptic_agent": usage},
    }


def diagnostic_rebuttal_agent(state: DebateState) -> dict[str, object]:
    if _should_skip(state, "diagnostic_rebuttal_agent"):
        return {
            "diagnostic_rebuttal": "(Fase de contrarréplica omitida por decisión del moderador.)",
            "history": [DebateMessage(role="diagnostic_rebuttal", content="(omitido)")],
            "tool_calls_log": [],
        }

    template = _template_for(state)
    effort = _resolve_effort(
        state, "diagnostic_reasoning_effort", state["diagnostic_model"],
        "diagnostic_rebuttal_agent", "Contrarréplica",
    )
    prompt_history, history_summary, mode = _prompt_history(state, exclude_current_round=True)
    content, tool_log, usage = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2, reasoning_effort=effort),
        "diagnostic_rebuttal_agent",
        template.diagnostic_system,
        rebuttal_prompt(
            state["topic"], state["context"], state["diagnostic_response"],
            state["skeptic_response"], state.get("hypotheses", []),
            history=prompt_history,
            language=state.get("language", "es"),
            history_summary=history_summary,
            mode=mode,
        ),
        run_id=state.get("run_id", ""),
        round_number=state["round"],
    )
    return {
        "diagnostic_rebuttal": content,
        "history": [DebateMessage(role="diagnostic_rebuttal", content=content)],
        "tool_calls_log": tool_log,
        "token_usage": {"diagnostic_rebuttal_agent": usage},
    }


def _parse_moderator_response(text: str) -> ModeratorDecision:
    """Extract JSON from the model response and parse into ModeratorDecision.

    Handles fenced JSON, Markdown-wrapped JSON (headers before/after),
    and naked JSON objects.
    """
    cleaned = text.strip()

    # Strategy 1: Look for ```json or ``` fences
    for pattern in [r"```json\s*(\{.*\})\s*```", r"```\s*(\{.*\})\s*```"]:
        match = re.search(pattern, cleaned, re.DOTALL)
        if match:
            try:
                return ModeratorDecision.model_validate_json(match.group(1))
            except Exception:
                pass  # Try next strategy

    # Strategy 2: Find outermost JSON object via brace counting
    # This handles cases like "## Header\n{...}\nmore text"
    start = -1
    depth = 0
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = cleaned[start:i + 1]
                try:
                    return ModeratorDecision.model_validate_json(candidate)
                except Exception:
                    start = -1  # Continue searching

    # Strategy 3: First { to last } (last resort, may be wrong)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in moderator response: {text[:200]}")
    return ModeratorDecision.model_validate_json(cleaned[start:end + 1])


def _usage_from_message(msg: object) -> dict[str, int]:
    raw_usage = getattr(msg, "usage_metadata", None) or {}
    return {
        "input_tokens":  raw_usage.get("input_tokens",  0) or 0,
        "output_tokens": raw_usage.get("output_tokens", 0) or 0,
        "total_tokens":  raw_usage.get("total_tokens",  0) or 0,
    }


def _decision_from_structured_result(result: object) -> tuple[ModeratorDecision | None, dict[str, int]]:
    """Extract a ModeratorDecision (and token usage) from a with_structured_output result.

    With include_raw=True langchain returns {"raw", "parsed", "parsing_error"}.
    When the model wraps the JSON in code fences, ``parsed`` is None but ``raw``
    holds the full response — rescue it with the fence-aware parser instead of
    re-invoking the model (which would double the moderator's cost).
    """
    if isinstance(result, ModeratorDecision):
        return result, {}
    if not isinstance(result, dict):
        return None, {}

    raw_msg = result.get("raw")
    usage = _usage_from_message(raw_msg) if raw_msg is not None else {}
    parsed = result.get("parsed")
    if isinstance(parsed, ModeratorDecision):
        return parsed, usage

    raw_text = _message_content(raw_msg) if raw_msg is not None else ""
    if raw_text.strip():
        try:
            return _parse_moderator_response(raw_text), usage
        except Exception as exc:  # noqa: BLE001
            _log.warning("moderator raw rescue failed (%s)", exc)
    return None, {}


def moderator_agent(state: DebateState) -> dict[str, object]:
    control = get_control(state.get("run_id", ""))
    if control is not None:
        control.raise_if_cancelled()

    template = _template_for(state)
    language = state.get("language", "es")
    effort = _resolve_effort(
        state, "moderator_reasoning_effort", state["moderator_model"],
        "moderator_agent", "Moderador",
    )
    model = create_github_model(state["moderator_model"], temperature=0.0, reasoning_effort=effort)

    prompt_history, history_summary, mode = _prompt_history(state, exclude_current_round=True)
    prompt = moderator_prompt(
        state["topic"],
        state["context"],
        state["round"],
        state["max_rounds"],
        state["confidence_threshold"],
        state.get("early_out_threshold", state["confidence_threshold"]),
        state["diagnostic_response"],
        state["skeptic_response"],
        state["diagnostic_rebuttal"],
        hypotheses=state.get("hypotheses", []),
        history=prompt_history,
        history_summary=history_summary,
        language=language,
        mode=mode,
    )

    decision: ModeratorDecision | None = None
    mod_usage: dict[str, int] = {}
    try:
        structured = model.with_structured_output(ModeratorDecision, include_raw=True)
        result = structured.invoke(
            [
                SystemMessage(content=template.moderator_system),
                HumanMessage(content=prompt),
            ]
        )
        decision, mod_usage = _decision_from_structured_result(result)
        if decision is None:
            _log.warning(
                "moderator structured output unusable (parsed missing and raw rescue failed), "
                "falling back to plain-text path",
            )
    except Exception as exc:
        _log.warning("moderator structured output failed (%s), falling back to plain-text path", exc)
        decision = None

    if decision is None:
        response = model.invoke(
            [
                SystemMessage(content=template.moderator_system),
                HumanMessage(content=prompt + moderator_json_fallback_suffix(language)),
            ]
        )
        decision = _parse_moderator_response(_message_content(response))
        fallback_usage = getattr(response, "usage_metadata", None) or {}
        mod_usage = {
            "input_tokens":  fallback_usage.get("input_tokens",  0) or 0,
            "output_tokens": fallback_usage.get("output_tokens", 0) or 0,
            "total_tokens":  fallback_usage.get("total_tokens",  0) or 0,
        }

    next_round = state["round"] + 1 if decision.status == "continue" else state["round"]
    return {
        "moderator_decision": decision,
        "round": next_round,
        "history": [DebateMessage(role="moderator", content=decision.model_dump_json(indent=2))],
        "token_usage": {"moderator_agent": mod_usage},
    }


def summarize_history(state: DebateState) -> dict[str, object]:
    """Compress finished rounds into a cumulative summary (runs after each round)."""
    compress = state.get("compress_history", True)
    history = state.get("history", [])

    # The moderator already incremented state["round"] on "continue", so the
    # just-finished round is derived from the history itself: one moderator
    # message marks the end of each round.
    finished_round = sum(1 for m in history if m.role == "moderator")
    if not compress or finished_round < 1 or not history:
        return {}

    last_msgs = _last_round_messages(history, finished_round)
    if not last_msgs:
        return {}

    previous_summary = state.get("history_summary", "")
    previous_block = (
        f"Resumen acumulado de las rondas anteriores:\n{previous_summary}\n\n"
        if previous_summary else ""
    )
    summary_prompt_text = (
        "Resume el estado de un debate de diagnóstico técnico en un párrafo "
        "conciso (máximo 400 palabras) que capture:\n"
        "1. Las hipótesis principales discutidas y su estado.\n"
        "2. La evidencia clave presentada.\n"
        "3. Los puntos de desacuerdo o incertidumbre pendientes.\n"
        "No incluyas detalles de implementación del debate; enfócate en el contenido técnico.\n"
        "Integra el resumen acumulado previo (si existe) con los mensajes nuevos en un único resumen.\n\n"
        + previous_block
        + "Mensajes nuevos a integrar:\n"
        + "\n\n".join(f"[{m.role}]\n{m.content[:2000]}" for m in last_msgs)
    )

    summary_model_name = state.get("summary_model", state["moderator_model"])
    summary_text = ""
    sum_usage: dict[str, int] = {}
    try:
        summary_model = create_github_model(summary_model_name, temperature=0.3)
        summary_response = summary_model.invoke([HumanMessage(content=summary_prompt_text)])
        summary_text = _message_content(summary_response)
        raw_usage = getattr(summary_response, "usage_metadata", None) or {}
        sum_usage = {
            "input_tokens":  raw_usage.get("input_tokens",  0) or 0,
            "output_tokens": raw_usage.get("output_tokens", 0) or 0,
            "total_tokens":  raw_usage.get("total_tokens",  0) or 0,
        }
    except Exception as exc:
        _log.warning("History summarization failed (%s), keeping full history", exc)
        return {}

    round_log_entry = DebateRound(
        round=finished_round,
        diagnostic=state.get("diagnostic_response", "")[:500],
        skeptic=state.get("skeptic_response", "")[:500],
        rebuttal=state.get("diagnostic_rebuttal", "")[:500],
        moderator=json.loads(state["moderator_decision"].model_dump_json() if state.get("moderator_decision") else "{}"),
    )

    return {
        "history_summary": summary_text,
        "round_log": [round_log_entry],
        "_summarize_event": {
            "type": "history_compressed",
            "round": finished_round,
            "summary": summary_text,
        },
        "token_usage": {"summarize_history": sum_usage},
    }


def user_input_gate(state: DebateState) -> dict[str, object]:
    """Human-in-the-loop pause between rounds."""
    control = get_control(state.get("run_id", ""))
    if control is None or not control.pause_between_rounds:
        return {}
    comment = control.wait_for_comment(state["round"])
    if comment:
        return {"history": [DebateMessage(role="user", content=comment)]}
    return {}


def finalize(state: DebateState) -> dict[str, object]:
    decision = state["moderator_decision"]
    if decision is None:
        return {"final_result": "El debate terminó sin decisión del moderador."}

    hypotheses_section = _format_list(
        [f"{h.id} [{h.state}]: {h.text}" for h in state.get("hypotheses", [])]
    ) if state.get("hypotheses") else "- Ninguna."

    result = f"""
Estado: {decision.status}
Confianza: {decision.confidence:.2f}
Riesgo: {decision.risk_level}

Hipótesis principal:
{decision.leading_hypothesis or "No determinada."}

Hipótesis en debate:
{hypotheses_section}

Evidencia:
{_format_list(decision.evidence)}

Evidencia faltante:
{_format_list(decision.missing_evidence)}

Hipótesis rechazadas:
{_format_list(decision.rejected_hypotheses)}

Siguiente paso:
{decision.next_step}

Fix recomendado:
{decision.recommended_fix or "No recomendado todavía."}

Validación:
{_format_list(decision.validation)}

Motivo de cierre:
{decision.stop_reason or "No especificado."}
""".strip()
    return {"final_result": result}


def _format_list(items: list[str]) -> str:
    if not items:
        return "- Ninguno."
    return "\n".join(f"- {item}" for item in items)


# Statuses that mean "run another debate round".
# - "continue":        moderator explicitly wants another round
# - "needs_more_data": diagnosis is incomplete, gather more evidence next round
_CONTINUE_STATUSES = {"continue", "needs_more_data"}


def route_after_moderator(state: DebateState) -> str:
    decision = state["moderator_decision"]
    if decision is None:
        return "finalize"
    if state["round"] > state["max_rounds"]:
        return "finalize"
    if decision.status not in _CONTINUE_STATUSES:
        return "finalize"
    return "summarize_history"


def route_after_summary(state: DebateState) -> str:
    """After summarization, check if human-in-the-loop is enabled."""
    control = get_control(state.get("run_id", ""))
    if control is not None and control.pause_between_rounds:
        return "user_input_gate"
    return "diagnostic_agent"


def build_graph():
    builder = StateGraph(DebateState)

    builder.add_node("diagnostic_agent", diagnostic_agent)
    builder.add_node("skeptic_agent", skeptic_agent)
    builder.add_node("diagnostic_rebuttal_agent", diagnostic_rebuttal_agent)
    builder.add_node("moderator_agent", moderator_agent)
    builder.add_node("summarize_history", summarize_history)
    builder.add_node("user_input_gate", user_input_gate)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "diagnostic_agent")
    builder.add_edge("diagnostic_agent", "skeptic_agent")
    builder.add_edge("skeptic_agent", "diagnostic_rebuttal_agent")
    builder.add_edge("diagnostic_rebuttal_agent", "moderator_agent")
    builder.add_conditional_edges(
        "moderator_agent",
        route_after_moderator,
        {
            "summarize_history": "summarize_history",
            "finalize": "finalize",
        },
    )
    builder.add_conditional_edges(
        "summarize_history",
        route_after_summary,
        {
            "user_input_gate": "user_input_gate",
            "diagnostic_agent": "diagnostic_agent",
        },
    )
    builder.add_edge("user_input_gate", "diagnostic_agent")
    builder.add_edge("finalize", END)

    return builder.compile()


def create_initial_state(
    topic: str,
    context: str = "",
    diagnostic_model: str = "",
    skeptic_model: str = "",
    moderator_model: str = "",
    summary_model: str = "",
    run_id: str = "",
    template: str = "",
    language: str = "",
    initial_history: list[DebateMessage] | None = None,
    diagnostic_reasoning_effort: str = "",
    skeptic_reasoning_effort: str = "",
    moderator_reasoning_effort: str = "",
) -> DebateState:
    settings = get_settings()
    return {
        "topic": topic,
        "context": context,
        "round": 1,
        "max_rounds": settings.max_rounds,
        "confidence_threshold": settings.confidence_threshold,
        "early_out_threshold": getattr(settings, "early_out_threshold", settings.confidence_threshold),
        "diagnostic_response": "",
        "skeptic_response": "",
        "diagnostic_rebuttal": "",
        "moderator_decision": None,
        "history": list(initial_history or []),
        "tool_calls_log": [],
        "final_result": None,
        "hypotheses": [],
        "history_summary": "",
        "round_log": [],
        "early_out_recommended": False,
        "early_out_confidence": 0.0,
        "early_out_rationale": "",
        "diagnostic_model": diagnostic_model or settings.diagnostic_model,
        "skeptic_model": skeptic_model or settings.skeptic_model,
        "moderator_model": moderator_model or settings.moderator_model,
        "summary_model": summary_model or getattr(settings, "summary_model", "") or settings.moderator_model,
        "diagnostic_reasoning_effort": diagnostic_reasoning_effort or settings.diagnostic_reasoning_effort,
        "skeptic_reasoning_effort": skeptic_reasoning_effort or settings.skeptic_reasoning_effort,
        "moderator_reasoning_effort": moderator_reasoning_effort or settings.moderator_reasoning_effort,
        "run_id": run_id,
        "template": template or settings.prompt_template,
        "language": language or settings.prompt_language,
        "compress_history": getattr(settings, "compress_history", True),
        "token_usage": {},
    }


def _graph_config(state: DebateState) -> dict:
    # 8 nodes per round (incl. summary + HITL gate) plus margin.
    return {"recursion_limit": state["max_rounds"] * 8 + 10}


def run_debate(
    topic: str,
    context: str = "",
    diagnostic_model: str = "",
    skeptic_model: str = "",
    moderator_model: str = "",
    summary_model: str = "",
    run_id: str = "",
    template: str = "",
    language: str = "",
    initial_history: list[DebateMessage] | None = None,
    diagnostic_reasoning_effort: str = "",
    skeptic_reasoning_effort: str = "",
    moderator_reasoning_effort: str = "",
    compress_history: bool | None = None,
    early_out_threshold: float | None = None,
) -> DebateState:
    graph = build_graph()
    initial_state = create_initial_state(
        topic,
        context,
        diagnostic_model=diagnostic_model,
        skeptic_model=skeptic_model,
        moderator_model=moderator_model,
        summary_model=summary_model,
        run_id=run_id,
        template=template,
        language=language,
        initial_history=initial_history,
        diagnostic_reasoning_effort=diagnostic_reasoning_effort,
        skeptic_reasoning_effort=skeptic_reasoning_effort,
        moderator_reasoning_effort=moderator_reasoning_effort,
    )
    if compress_history is not None:
        initial_state["compress_history"] = compress_history
    if early_out_threshold is not None:
        initial_state["early_out_threshold"] = early_out_threshold
    return graph.invoke(initial_state, _graph_config(initial_state))


def stream_debate_events(
    topic: str,
    context: str = "",
    diagnostic_model: str = "",
    skeptic_model: str = "",
    moderator_model: str = "",
    summary_model: str = "",
    run_id: str = "",
    template: str = "",
    language: str = "",
    initial_history: list[DebateMessage] | None = None,
    diagnostic_reasoning_effort: str = "",
    skeptic_reasoning_effort: str = "",
    moderator_reasoning_effort: str = "",
):
    graph = build_graph()
    initial_state = create_initial_state(
        topic,
        context,
        diagnostic_model=diagnostic_model,
        skeptic_model=skeptic_model,
        moderator_model=moderator_model,
        summary_model=summary_model,
        run_id=run_id,
        template=template,
        language=language,
        initial_history=initial_history,
        diagnostic_reasoning_effort=diagnostic_reasoning_effort,
        skeptic_reasoning_effort=skeptic_reasoning_effort,
        moderator_reasoning_effort=moderator_reasoning_effort,
    )

    yield {
        "type": "run_started",
        "round": initial_state["round"],
        "topic": topic,
        "max_rounds": initial_state["max_rounds"],
        "confidence_threshold": initial_state["confidence_threshold"],
        "template": initial_state["template"],
        "language": initial_state["language"],
    }

    # Accumulate token_usage from all node updates (reducer merges per-node dicts)
    accumulated_usage: dict[str, dict[str, int]] = {}

    # Updates are partial per-node dicts, so the merged hypothesis snapshot is
    # rebuilt here with the same reducer the graph state uses.
    hypotheses_snapshot: list[Hypothesis] = []
    current_round = initial_state["round"]

    for update in graph.stream(initial_state, _graph_config(initial_state), stream_mode="updates"):
        for node_name, node_update in update.items():
            if not node_update:
                continue

            # Accumulate token usage from this node update
            node_token_usage = node_update.get("token_usage") or {}
            for role, counts in node_token_usage.items():
                if role in accumulated_usage:
                    for k in ("input_tokens", "output_tokens", "total_tokens"):
                        accumulated_usage[role][k] = accumulated_usage[role].get(k, 0) + (counts.get(k, 0) or 0)
                else:
                    accumulated_usage[role] = {k: counts.get(k, 0) or 0
                                               for k in ("input_tokens", "output_tokens", "total_tokens")}

            # Agent text response
            if node_name in AGENT_EVENT_FIELDS:
                field_name, display_name = AGENT_EVENT_FIELDS[node_name]
                content = node_update.get(field_name, "")
                yield {
                    "type": "agent_completed",
                    "node": node_name,
                    "role": display_name,
                    "content": content,
                }
                if node_update.get("hypotheses"):
                    hypotheses_snapshot = _merge_hypotheses(hypotheses_snapshot, node_update["hypotheses"])
                    yield {
                        "type": "hypothesis_update",
                        "node": node_name,
                        "round": current_round,
                        "hypotheses": [h.model_dump(by_alias=True) for h in hypotheses_snapshot],
                    }
            elif node_name == "moderator_agent":
                decision = node_update.get("moderator_decision")
                if isinstance(decision, ModeratorDecision):
                    decision_payload = decision.model_dump()
                else:
                    decision_payload = decision
                if node_update.get("round") is not None:
                    current_round = node_update["round"]
                yield {
                    "type": "moderator_decision",
                    "node": node_name,
                    "decision": decision_payload,
                    "round": node_update.get("round"),
                }
            elif node_name == "summarize_history":
                ev = node_update.get("_summarize_event")
                if ev:
                    yield ev
            elif node_name == "finalize":
                yield {
                    "type": "final_result",
                    "node": node_name,
                    "content": node_update.get("final_result", ""),
                }

    # Compute totals and cost estimate, then include in run_finished
    total_input  = sum(u.get("input_tokens",  0) for u in accumulated_usage.values())
    total_output = sum(u.get("output_tokens", 0) for u in accumulated_usage.values())
    total_all    = sum(u.get("total_tokens",  0) for u in accumulated_usage.values())
    token_totals: dict = {}
    cost_estimate: dict | None = None
    if accumulated_usage:
        token_totals = {
            "by_node": accumulated_usage,
            "total": {
                "input_tokens":  total_input,
                "output_tokens": total_output,
                "total_tokens":  total_all if total_all else (total_input + total_output),
            },
        }
        # Build model mapping for cost estimation
        models_by_role = {
            "diagnostic_agent":         initial_state["diagnostic_model"],
            "skeptic_agent":            initial_state["skeptic_model"],
            "diagnostic_rebuttal_agent": initial_state["diagnostic_model"],
            "moderator_agent":          initial_state["moderator_model"],
            "summarize_history":        initial_state["summary_model"],
        }
        try:
            settings = get_settings()
            cost_estimate = estimate_cost(
                accumulated_usage,
                models_by_role,
                prices_file=getattr(settings, "model_prices_file", None),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Cost estimation failed (%s)", exc)

    yield {"type": "run_finished", "token_totals": token_totals, "cost_estimate": cost_estimate}
