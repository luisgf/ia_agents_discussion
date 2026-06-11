import json
import re
import time
import uuid

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agents_discussion.audit import audit_tool_call
from agents_discussion.config import get_settings
from agents_discussion.models import create_github_model
from agents_discussion.prompt_store import PromptTemplate, get_template
from agents_discussion.prompts import (
    diagnostic_prompt,
    moderator_json_fallback_suffix,
    moderator_prompt,
    rebuttal_prompt,
    skeptic_prompt,
)
from agents_discussion.runtime import RunCancelled, get_control
from agents_discussion.state import DebateMessage, DebateState, ModeratorDecision, ToolCallEntry
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


def _chunk_text(content: object) -> str:
    """Extract the plain-text part of a streamed chunk's content.

    Content may be a string (OpenAI-style) or a list of content blocks
    (e.g. [{"type": "text", "text": "..."}]) depending on the provider.
    """
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
    """Invoke the model streaming token deltas to the UI via control.emit.

    Emits agent_turn_started once, then one agent_delta per text chunk, and
    returns the aggregated response message (tool calls included). Falls back
    to a blocking invoke when the endpoint rejects streaming before producing
    any chunk. CLI runs (control is None) always use the blocking path.
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
    except Exception:  # noqa: BLE001 — endpoint without streaming support
        if response is not None:
            raise  # failed mid-stream: re-invoking would duplicate the turn
        return model.invoke(messages)
    if response is None:
        return model.invoke(messages)
    return response


# ── ReAct loop ───────────────────────────────────────────────────────────────


def _run_with_tools(
    model_factory,
    agent_node: str,
    system_prompt: str,
    user_message: str,
    run_id: str = "",
) -> tuple[str, list[ToolCallEntry]]:
    """Run a model in a ReAct loop: LLM → tool call(s) → LLM → … → final text.

    Returns the agent's final text response and a list of ToolCallEntry records.
    Tools are only used when TOOLS_ENABLED=true in settings. When the run has a
    registered RunControl (web runs), gated tools block until the operator
    approves them, and cancellation aborts the loop between LLM calls.
    """
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
            break  # LLM gave a final answer — exit loop

        # Emit the reasoning text this turn produced BEFORE the tool calls so
        # the UI shows: reasoning → tool(s) → reasoning → tool(s) → … → final.
        # Only emit when there is actual text content (some models return "" here).
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
                # Respond remaining tool_call_ids with a placeholder so the
                # conversation stays valid from the API's perspective.
                messages.append(
                    ToolMessage(content="Tool call limit reached.", tool_call_id=tc["id"])
                )
                continue
            call_count += 1

            tool_name: str = tc["name"]
            tool_args: dict = tc["args"]
            call_id: str = tc["id"]
            # UI correlation id: tool_call_started and tool_call share it so the
            # frontend can update the running card in place with the result.
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
                except Exception as exc:  # noqa: BLE001
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
            # Emit the tool_call event in real time so the UI shows it
            # immediately after execution, before the agent's next LLM call.
            # RunControl.emit is thread-safe (web.py: RunSession.publish uses a lock).
            # CLI runs (control is None) don't use SSE so no emit is needed there.
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

        # Recovery message and error-limit check go OUTSIDE the for loop so that
        # every tool_call_id in the batch already has its ToolMessage.
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
                break  # too many consecutive error batches — force final answer
        else:
            consecutive_errors = 0

    content = _message_content(response)
    return content, tool_log


# ── Agent nodes ──────────────────────────────────────────────────────────────


def diagnostic_agent(state: DebateState) -> dict[str, object]:
    template = _template_for(state)
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2),
        "diagnostic_agent",
        template.diagnostic_system,
        diagnostic_prompt(
            state["topic"], state["context"], state["round"], state["history"],
            language=state.get("language", "es"),
        ),
        run_id=state.get("run_id", ""),
    )
    return {
        "diagnostic_response": content,
        "history": [DebateMessage(role="diagnostic_agent", content=content)],
        "tool_calls_log": tool_log,
    }


def skeptic_agent(state: DebateState) -> dict[str, object]:
    template = _template_for(state)
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["skeptic_model"], temperature=0.1),
        "skeptic_agent",
        template.skeptic_system,
        skeptic_prompt(
            state["topic"], state["context"], state["diagnostic_response"], state["history"],
            language=state.get("language", "es"),
        ),
        run_id=state.get("run_id", ""),
    )
    return {
        "skeptic_response": content,
        "history": [DebateMessage(role="skeptic_agent", content=content)],
        "tool_calls_log": tool_log,
    }


def diagnostic_rebuttal_agent(state: DebateState) -> dict[str, object]:
    template = _template_for(state)
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2),
        "diagnostic_rebuttal_agent",
        template.diagnostic_system,
        rebuttal_prompt(
            state["topic"], state["context"], state["diagnostic_response"],
            state["skeptic_response"], language=state.get("language", "es"),
        ),
        run_id=state.get("run_id", ""),
    )
    return {
        "diagnostic_rebuttal": content,
        "history": [DebateMessage(role="diagnostic_rebuttal", content=content)],
        "tool_calls_log": tool_log,
    }


def _parse_moderator_response(text: str) -> ModeratorDecision:
    """Extract JSON from the model response and parse it into a ModeratorDecision.

    Handles both raw JSON and JSON wrapped in a markdown code block.
    """
    # Strip markdown code fences if present
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1)
    else:
        # Find the outermost JSON object in the text
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"No JSON object found in moderator response: {text[:200]}")
        json_str = text[start : end + 1]

    data = json.loads(json_str)
    return ModeratorDecision.model_validate(data)


def moderator_agent(state: DebateState) -> dict[str, object]:
    control = get_control(state.get("run_id", ""))
    if control is not None:
        control.raise_if_cancelled()

    template = _template_for(state)
    language = state.get("language", "es")
    model = create_github_model(state["moderator_model"], temperature=0.0)
    prompt = moderator_prompt(
        state["topic"],
        state["context"],
        state["round"],
        state["max_rounds"],
        state["confidence_threshold"],
        state["diagnostic_response"],
        state["skeptic_response"],
        state["diagnostic_rebuttal"],
        history=state["history"],
        language=language,
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
    except Exception:  # noqa: BLE001 — model/endpoint without tool-call support
        decision = None

    if decision is None:
        # Fallback: plain text completion + JSON extraction (legacy path).
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


def user_input_gate(state: DebateState) -> dict[str, object]:
    """Human-in-the-loop pause between rounds (web runs with the option enabled).

    Blocks until the operator submits a comment or skips; the comment is added
    to the debate history so the next round's agents can react to it.
    """
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

    result = f"""
Estado: {decision.status}
Confianza: {decision.confidence:.2f}
Riesgo: {decision.risk_level}

Hipótesis principal:
{decision.leading_hypothesis or "No determinada."}

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


def route_after_moderator(state: DebateState) -> str:
    decision = state["moderator_decision"]
    if decision is None:
        return "finalize"
    if state["round"] > state["max_rounds"]:
        return "finalize"
    if decision.confidence >= state["confidence_threshold"]:
        return "finalize"
    if decision.status != "continue":
        return "finalize"
    return "user_input_gate"


def build_graph():
    builder = StateGraph(DebateState)

    builder.add_node("diagnostic_agent", diagnostic_agent)
    builder.add_node("skeptic_agent", skeptic_agent)
    builder.add_node("diagnostic_rebuttal_agent", diagnostic_rebuttal_agent)
    builder.add_node("moderator_agent", moderator_agent)
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
            "user_input_gate": "user_input_gate",
            "finalize": "finalize",
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
    run_id: str = "",
    template: str = "",
    language: str = "",
    initial_history: list[DebateMessage] | None = None,
) -> DebateState:
    settings = get_settings()
    return {
        "topic": topic,
        "context": context,
        "round": 1,
        "max_rounds": settings.max_rounds,
        "confidence_threshold": settings.confidence_threshold,
        "diagnostic_response": "",
        "skeptic_response": "",
        "diagnostic_rebuttal": "",
        "moderator_decision": None,
        "history": list(initial_history or []),
        "tool_calls_log": [],
        "final_result": None,
        "diagnostic_model": diagnostic_model or settings.diagnostic_model,
        "skeptic_model": skeptic_model or settings.skeptic_model,
        "moderator_model": moderator_model or settings.moderator_model,
        "run_id": run_id,
        "template": template or settings.prompt_template,
        "language": language or settings.prompt_language,
    }


def _graph_config(state: DebateState) -> dict:
    # 6 nodes per round (incl. the HITL gate) plus margin; the default
    # recursion limit (25) is too low for MAX_ROUNDS >= 5.
    return {"recursion_limit": state["max_rounds"] * 6 + 10}


def run_debate(topic: str, context: str = "") -> DebateState:
    graph = build_graph()
    initial_state = create_initial_state(topic, context)
    return graph.invoke(initial_state, _graph_config(initial_state))


def stream_debate_events(
    topic: str,
    context: str = "",
    diagnostic_model: str = "",
    skeptic_model: str = "",
    moderator_model: str = "",
    run_id: str = "",
    template: str = "",
    language: str = "",
    initial_history: list[DebateMessage] | None = None,
):
    graph = build_graph()
    initial_state = create_initial_state(
        topic,
        context,
        diagnostic_model=diagnostic_model,
        skeptic_model=skeptic_model,
        moderator_model=moderator_model,
        run_id=run_id,
        template=template,
        language=language,
        initial_history=initial_history,
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
            # ── Tool calls are emitted in real time from _run_with_tools via
            # control.emit, right after each tool actually executes. The
            # tool_calls_log is still kept in state for persistence and replay,
            # but we no longer re-emit it here to avoid duplicates.

            # ── Agent text response ──
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
            elif node_name == "finalize":
                yield {
                    "type": "final_result",
                    "node": node_name,
                    "content": node_update.get("final_result", ""),
                }

    yield {"type": "run_finished"}
