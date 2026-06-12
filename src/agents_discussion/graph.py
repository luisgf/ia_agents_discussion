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
from agents_discussion.prompt_store import PromptTemplate, get_template
from agents_discussion.prompts import (
    diagnostic_prompt,
    moderator_json_fallback_suffix,
    moderator_prompt,
    rebuttal_prompt,
    skeptic_prompt,
)
from agents_discussion.runtime import RunCancelled, get_control
from agents_discussion.state import (
    DebateMessage,
    DebateRound,
    DebateState,
    FlowDirective,
    Hypothesis,
    ModeratorDecision,
    ToolCallEntry,
)
from agents_discussion.tools import get_tools


AGENT_EVENT_FIELDS = {
    "diagnostic_agent": ("diagnostic_response", "Diagnóstico Principal"),
    "skeptic_agent": ("skeptic_response", "Revisor Escéptico"),
    "diagnostic_rebuttal_agent": ("diagnostic_rebuttal", "Contrarréplica"),
}


def _message_content(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
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


def _invoke_streaming(model, messages: list, control, agent_node: str, agent_role: str):
    """Invoke model with streaming to UI. Falls back to blocking invoke."""
    if control is None:
        return model.invoke(messages)

    control.emit({
        "type": "agent_turn_started",
        "agent_node": agent_node,
        "agent_role": agent_role,
    })
    response = None
    try:
        for chunk in model.stream(messages):
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
    r"###\s*HYPOTHESIS-([A-Za-z0-9_-]+)\s*\n\s*Text:\s*(.+?)(?=\n###|\n##|\Z)",
    re.IGNORECASE | re.DOTALL,
)

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


def _extract_hypotheses(text: str, proposer: str, round_number: int) -> list[Hypothesis]:
    """Extract structured hypotheses from agent text response."""
    hypotheses: list[Hypothesis] = []
    matches = list(_HYPOTHESIS_RE.finditer(text))
    # If no structured format found, fall back to extracting the first bold/heading line
    if not matches:
        # Try to find a leading hypothesis in the first paragraph
        first_para = text.split("\n\n")[0] if text else ""
        if first_para:
            hypotheses.append(Hypothesis(
                id="H-1",
                text=first_para.strip(),
                state="active",
                proposer=proposer,
                round=round_number,
            ))
        return hypotheses

    for i, match in enumerate(matches):
        hyp_id = match.group(1).strip()
        hyp_text = match.group(2).strip()
        hypotheses.append(Hypothesis(
            id=hyp_id,
            text=hyp_text,
            state="active",
            proposer=proposer,
            round=round_number,
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


def _build_compressed_history_prompt(
    summary: str,
    last_messages: list[DebateMessage],
    language: str = "es",
) -> str:
    """Build the history section for a prompt using compression."""
    from agents_discussion.prompts import format_history
    return format_history(
        [],  # We don't pass full history when compressed
        language=language,
        history_summary=summary,
        last_round_messages=last_messages,
        mode="compressed",
    )


# ── ReAct loop (unchanged core) ─────────────────────────────────────────────

def _run_with_tools(
    model_factory,
    agent_node: str,
    system_prompt: str,
    user_message: str,
    run_id: str = "",
) -> tuple[str, list[ToolCallEntry]]:
    """Run a model in a ReAct loop: LLM → tool call(s) → LLM → … → final text."""
    settings = get_settings()
    control = get_control(run_id)
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

    while call_count <= settings.max_tool_calls_per_agent:
        if control is not None:
            control.raise_if_cancelled()

        response = _invoke_streaming(model, messages, control, agent_node, agent_role)
        messages.append(response)

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
            if tool_fn is None:
                result = f"Unknown tool: {tool_name}"
                error = True
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
    return content, tool_log


# ── Agent nodes ──────────────────────────────────────────────────────────────


def _history_mode(state: DebateState) -> tuple[str, str]:
    """Determine history mode and summary for prompts."""
    history = state.get("history", [])
    summary = state.get("history_summary", "")
    current_round = state.get("round", 1)
    compress = state.get("compress_history", True)

    if not compress or current_round <= 2 or not summary:
        return "", "full"

    last_msgs = _last_round_messages(history, current_round)
    return summary, "compressed" if last_msgs else "full"


def diagnostic_agent(state: DebateState) -> dict[str, object]:
    template = _template_for(state)
    effort = _resolve_effort(
        state, "diagnostic_reasoning_effort", state["diagnostic_model"],
        "diagnostic_agent", "Diagnóstico Principal",
    )
    history_summary, mode = _history_mode(state)
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2, reasoning_effort=effort),
        "diagnostic_agent",
        template.diagnostic_system,
        diagnostic_prompt(
            state["topic"], state["context"], state["round"], state["history"],
            language=state.get("language", "es"),
            history_summary=history_summary,
            mode=mode,
        ),
        run_id=state.get("run_id", ""),
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
    history_summary, mode = _history_mode(state)
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["skeptic_model"], temperature=0.1, reasoning_effort=effort),
        "skeptic_agent",
        template.skeptic_system,
        skeptic_prompt(
            state["topic"], state["context"], state["diagnostic_response"],
            state.get("hypotheses", []), state["history"],
            language=state.get("language", "es"),
            history_summary=history_summary,
            mode=mode,
        ),
        run_id=state.get("run_id", ""),
    )

    # Update hypothesis states based on skeptic feedback
    # Simple heuristic: if skeptic mentions rejecting a hypothesis by ID, update it
    updated_hypotheses: list[Hypothesis] = []
    for h in state.get("hypotheses", []):
        new_h = h.model_copy() if hasattr(h, "model_copy") else Hypothesis(**h.model_dump())
        if f"[hypothesis:{h.id}]" in content or f"HYPOTHESIS-{h.id}" in content:
            idx = content.find(f"[hypothesis:{h.id}]")
            if idx == -1:
                idx = content.find(f"HYPOTHESIS-{h.id}")
            snippet = content[idx:idx + 500] if idx >= 0 else ""
            if re.search(r"\brejected\b|\brechazada?\b|\binválida?\b", snippet, re.IGNORECASE):
                new_h.state = "rejected"
                reason_match = re.search(r"[Rr]eason:\s*(.+?)(?=\n\n|\Z)", snippet, re.DOTALL)
                new_h.rejected_reason = reason_match.group(1).strip() if reason_match else "Rechazada por el escéptico."
            elif re.search(r"\baccepted\b|\baceptada?\b|\bconfirmada?\b", snippet, re.IGNORECASE):
                new_h.state = "confirmed"
        updated_hypotheses.append(new_h)

    return {
        "skeptic_response": content,
        "history": [DebateMessage(role="skeptic_agent", content=content)],
        "tool_calls_log": tool_log,
        "hypotheses": updated_hypotheses,
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
    history_summary, mode = _history_mode(state)
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2, reasoning_effort=effort),
        "diagnostic_rebuttal_agent",
        template.diagnostic_system,
        rebuttal_prompt(
            state["topic"], state["context"], state["diagnostic_response"],
            state["skeptic_response"], state.get("hypotheses", []),
            history=state["history"],
            language=state.get("language", "es"),
            history_summary=history_summary,
            mode=mode,
        ),
        run_id=state.get("run_id", ""),
    )
    return {
        "diagnostic_rebuttal": content,
        "history": [DebateMessage(role="diagnostic_rebuttal", content=content)],
        "tool_calls_log": tool_log,
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

    history_summary, mode = _history_mode(state)
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
        history=state["history"],
        history_summary=history_summary,
        language=language,
        mode=mode,
    )

    decision: ModeratorDecision | None = None
    try:
        structured = model.with_structured_output(ModeratorDecision)
        result = structured.invoke(
            [
                SystemMessage(content=template.moderator_system),
                HumanMessage(content=prompt),
            ]
        )
        if isinstance(result, ModeratorDecision):
            decision = result
        else:
            _log.warning(
                "moderator structured output returned %s (expected ModeratorDecision), "
                "falling back to plain-text path",
                type(result).__name__,
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

    next_round = state["round"] + 1 if decision.status == "continue" else state["round"]
    return {
        "moderator_decision": decision,
        "round": next_round,
        "history": [DebateMessage(role="moderator", content=decision.model_dump_json(indent=2))],
    }


def summarize_history(state: DebateState) -> dict[str, object]:
    """Compress history after each round when enabled and round > 2."""
    compress = state.get("compress_history", True)
    current_round = state["round"]
    history = state.get("history", [])

    if not compress or current_round <= 2 or not history:
        return {}

    last_msgs = _last_round_messages(history, current_round)
    if not last_msgs:
        return {}

    summary_prompt_text = (
        "Resume los siguientes mensajes de un debate de diagnóstico técnico "
        "en un párrafo conciso (máximo 400 palabras) que capture:\n"
        "1. Las hipótesis principales discutidas y su estado.\n"
        "2. La evidencia clave presentada.\n"
        "3. Los puntos de desacuerdo o incertidumbre pendientes.\n"
        "No incluyas detalles de implementación del debate; enfócate en el contenido técnico.\n\n"
        "Mensajes a resumir:\n"
        + "\n\n".join(f"[{m.role}]\n{m.content[:2000]}" for m in last_msgs)
    )

    summary_model_name = state.get("summary_model", state["moderator_model"])
    summary_text = ""
    try:
        summary_model = create_github_model(summary_model_name, temperature=0.3)
        summary_response = summary_model.invoke([HumanMessage(content=summary_prompt_text)])
        summary_text = _message_content(summary_response)
    except Exception as exc:
        _log.warning("History summarization failed (%s), keeping full history", exc)
        return {}

    round_log_entry = DebateRound(
        round=current_round,
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
            "round": current_round,
            "summary": summary_text,
        },
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

    for update in graph.stream(initial_state, _graph_config(initial_state), stream_mode="updates"):
        for node_name, node_update in update.items():
            if not node_update:
                continue

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
            elif node_name == "moderator_agent":
                decision = node_update.get("moderator_decision")
                if isinstance(decision, ModeratorDecision):
                    decision_payload = decision.model_dump()
                else:
                    decision_payload = decision
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

    yield {"type": "run_finished"}
