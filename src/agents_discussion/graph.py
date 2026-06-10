from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from agents_discussion.config import get_settings
from agents_discussion.models import (
    create_diagnostic_model,
    create_moderator_model,
    create_skeptic_model,
)
from agents_discussion.prompts import (
    DIAGNOSTIC_SYSTEM_PROMPT,
    MODERATOR_SYSTEM_PROMPT,
    SKEPTIC_SYSTEM_PROMPT,
    diagnostic_prompt,
    moderator_prompt,
    rebuttal_prompt,
    skeptic_prompt,
)
from agents_discussion.state import DebateMessage, DebateState, ModeratorDecision


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


def diagnostic_agent(state: DebateState) -> dict[str, object]:
    model = create_diagnostic_model()
    response = model.invoke(
        [
            SystemMessage(content=DIAGNOSTIC_SYSTEM_PROMPT),
            HumanMessage(
                content=diagnostic_prompt(
                    state["topic"],
                    state["context"],
                    state["round"],
                    state["history"],
                )
            ),
        ]
    )
    content = _message_content(response)
    return {
        "diagnostic_response": content,
        "history": [DebateMessage(role="diagnostic_agent", content=content)],
    }


def skeptic_agent(state: DebateState) -> dict[str, object]:
    model = create_skeptic_model()
    response = model.invoke(
        [
            SystemMessage(content=SKEPTIC_SYSTEM_PROMPT),
            HumanMessage(
                content=skeptic_prompt(
                    state["topic"],
                    state["context"],
                    state["diagnostic_response"],
                    state["history"],
                )
            ),
        ]
    )
    content = _message_content(response)
    return {
        "skeptic_response": content,
        "history": [DebateMessage(role="skeptic_agent", content=content)],
    }


def diagnostic_rebuttal_agent(state: DebateState) -> dict[str, object]:
    model = create_diagnostic_model()
    response = model.invoke(
        [
            SystemMessage(content=DIAGNOSTIC_SYSTEM_PROMPT),
            HumanMessage(
                content=rebuttal_prompt(
                    state["topic"],
                    state["context"],
                    state["diagnostic_response"],
                    state["skeptic_response"],
                )
            ),
        ]
    )
    content = _message_content(response)
    return {
        "diagnostic_rebuttal": content,
        "history": [DebateMessage(role="diagnostic_rebuttal", content=content)],
    }


def moderator_agent(state: DebateState) -> dict[str, object]:
    model = create_moderator_model().with_structured_output(ModeratorDecision)
    decision = model.invoke(
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

    if not isinstance(decision, ModeratorDecision):
        decision = ModeratorDecision.model_validate(decision)

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


def create_initial_state(topic: str, context: str = "") -> DebateState:
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
        "final_result": None,
    }


def run_debate(topic: str, context: str = "") -> DebateState:
    graph = build_graph()
    initial_state = create_initial_state(topic, context)
    return graph.invoke(initial_state)


def stream_debate_events(topic: str, context: str = ""):
    graph = build_graph()
    initial_state = create_initial_state(topic, context)

    yield {
        "type": "run_started",
        "round": initial_state["round"],
        "topic": topic,
        "max_rounds": initial_state["max_rounds"],
        "confidence_threshold": initial_state["confidence_threshold"],
    }

    for update in graph.stream(initial_state, stream_mode="updates"):
        for node_name, node_update in update.items():
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
