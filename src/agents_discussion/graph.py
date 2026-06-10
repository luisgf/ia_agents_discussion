import json
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agents_discussion.config import get_settings
from agents_discussion.models import create_github_model
from agents_discussion.prompts import (
    DIAGNOSTIC_SYSTEM_PROMPT,
    MODERATOR_SYSTEM_PROMPT,
    SKEPTIC_SYSTEM_PROMPT,
    diagnostic_prompt,
    moderator_prompt,
    rebuttal_prompt,
    skeptic_prompt,
)
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


# ── ReAct loop ───────────────────────────────────────────────────────────────


def _run_with_tools(
    model_factory,
    agent_node: str,
    system_prompt: str,
    user_message: str,
) -> tuple[str, list[ToolCallEntry]]:
    """Run a model in a ReAct loop: LLM → tool call(s) → LLM → … → final text.

    Returns the agent's final text response and a list of ToolCallEntry records.
    Tools are only used when TOOLS_ENABLED=true in settings.
    """
    settings = get_settings()
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

    while call_count <= settings.max_tool_calls_per_agent:
        response = model.invoke(messages)
        messages.append(response)

        raw_calls = getattr(response, "tool_calls", None) or []
        if not raw_calls:
            break  # LLM gave a final answer — exit loop

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

            tool_fn = tool_map.get(tool_name)
            if tool_fn is None:
                result = f"Unknown tool: {tool_name}"
                error = True
            else:
                try:
                    result = str(tool_fn.invoke(tool_args))
                    error = False
                except Exception as exc:  # noqa: BLE001
                    result = f"Tool error: {exc}"
                    error = True

            tool_log.append(
                ToolCallEntry(
                    agent=agent_node,
                    tool_name=tool_name,
                    args=tool_args,
                    result=result,
                    error=error,
                )
            )
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
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2),
        "diagnostic_agent",
        DIAGNOSTIC_SYSTEM_PROMPT,
        diagnostic_prompt(state["topic"], state["context"], state["round"], state["history"]),
    )
    return {
        "diagnostic_response": content,
        "history": [DebateMessage(role="diagnostic_agent", content=content)],
        "tool_calls_log": tool_log,
    }


def skeptic_agent(state: DebateState) -> dict[str, object]:
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["skeptic_model"], temperature=0.1),
        "skeptic_agent",
        SKEPTIC_SYSTEM_PROMPT,
        skeptic_prompt(state["topic"], state["context"], state["diagnostic_response"], state["history"]),
    )
    return {
        "skeptic_response": content,
        "history": [DebateMessage(role="skeptic_agent", content=content)],
        "tool_calls_log": tool_log,
    }


def diagnostic_rebuttal_agent(state: DebateState) -> dict[str, object]:
    content, tool_log = _run_with_tools(
        lambda: create_github_model(state["diagnostic_model"], temperature=0.2),
        "diagnostic_rebuttal_agent",
        DIAGNOSTIC_SYSTEM_PROMPT,
        rebuttal_prompt(state["topic"], state["context"], state["diagnostic_response"], state["skeptic_response"]),
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
    model = create_github_model(state["moderator_model"], temperature=0.0)
    response = model.invoke(
        [
            SystemMessage(content=MODERATOR_SYSTEM_PROMPT),
            HumanMessage(
                content=moderator_prompt(
                    state["topic"],
                    state["context"],
                    state["round"],
                    state["max_rounds"],
                    state["confidence_threshold"],
                    state["diagnostic_response"],
                    state["skeptic_response"],
                    state["diagnostic_rebuttal"],
                )
            ),
        ]
    )

    decision = _parse_moderator_response(_message_content(response))

    next_round = state["round"] + 1 if decision.status == "continue" else state["round"]
    return {
        "moderator_decision": decision,
        "round": next_round,
        "history": [DebateMessage(role="moderator", content=decision.model_dump_json(indent=2))],
    }


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
    return "diagnostic_agent"


def build_graph():
    builder = StateGraph(DebateState)

    builder.add_node("diagnostic_agent", diagnostic_agent)
    builder.add_node("skeptic_agent", skeptic_agent)
    builder.add_node("diagnostic_rebuttal_agent", diagnostic_rebuttal_agent)
    builder.add_node("moderator_agent", moderator_agent)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "diagnostic_agent")
    builder.add_edge("diagnostic_agent", "skeptic_agent")
    builder.add_edge("skeptic_agent", "diagnostic_rebuttal_agent")
    builder.add_edge("diagnostic_rebuttal_agent", "moderator_agent")
    builder.add_conditional_edges(
        "moderator_agent",
        route_after_moderator,
        {
            "diagnostic_agent": "diagnostic_agent",
            "finalize": "finalize",
        },
    )
    builder.add_edge("finalize", END)

    return builder.compile()


def create_initial_state(
    topic: str,
    context: str = "",
    diagnostic_model: str = "",
    skeptic_model: str = "",
    moderator_model: str = "",
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
        "history": [],
        "tool_calls_log": [],
        "final_result": None,
        "diagnostic_model": diagnostic_model or settings.diagnostic_model,
        "skeptic_model": skeptic_model or settings.skeptic_model,
        "moderator_model": moderator_model or settings.moderator_model,
    }


def run_debate(topic: str, context: str = "") -> DebateState:
    graph = build_graph()
    initial_state = create_initial_state(topic, context)
    return graph.invoke(initial_state)


def stream_debate_events(
    topic: str,
    context: str = "",
    diagnostic_model: str = "",
    skeptic_model: str = "",
    moderator_model: str = "",
):
    graph = build_graph()
    initial_state = create_initial_state(
        topic,
        context,
        diagnostic_model=diagnostic_model,
        skeptic_model=skeptic_model,
        moderator_model=moderator_model,
    )

    yield {
        "type": "run_started",
        "round": initial_state["round"],
        "topic": topic,
        "max_rounds": initial_state["max_rounds"],
        "confidence_threshold": initial_state["confidence_threshold"],
    }

    for update in graph.stream(initial_state, stream_mode="updates"):
        for node_name, node_update in update.items():
            # ── Tool calls produced by this node (emitted before the response) ──
            for tc in node_update.get("tool_calls_log") or []:
                yield {
                    "type": "tool_call",
                    "agent_node": tc["agent"],
                    "agent_role": AGENT_EVENT_FIELDS.get(tc["agent"], ("", tc["agent"]))[1],
                    "tool_name": tc["tool_name"],
                    "args": tc["args"],
                    "result": tc["result"],
                    "error": tc["error"],
                }

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
