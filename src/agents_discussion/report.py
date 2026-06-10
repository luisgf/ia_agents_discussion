"""Markdown report generation from a stored run record."""
from __future__ import annotations

import json

_STATUS_LABELS = {
    "running": "En curso",
    "completed": "Completado",
    "cancelled": "Detenido",
    "error": "Error",
    "interrupted": "Interrumpido",
}


def _fmt_list(items: list | None) -> str:
    if not items:
        return "- (ninguno)\n"
    return "".join(f"- {item}\n" for item in items)


def build_markdown_report(run: dict) -> str:
    """Render a complete debate run as a self-contained Markdown report."""
    lines: list[str] = []
    topic = run.get("topic", "")
    models = run.get("models") or {}

    lines.append(f"# Informe de diagnóstico: {topic}\n")
    lines.append(f"- **Run ID:** `{run.get('run_id', '')}`")
    lines.append(f"- **Fecha:** {run.get('timestamp', '—')}")
    lines.append(f"- **Estado:** {_STATUS_LABELS.get(run.get('status'), run.get('status', '—'))}")
    if run.get("parent_run_id"):
        lines.append(f"- **Reanudado desde:** `{run['parent_run_id']}`")
    if models:
        lines.append(
            f"- **Modelos:** diagnóstico `{models.get('diagnostic', '—')}` · "
            f"escéptico `{models.get('skeptic', '—')}` · moderador `{models.get('moderator', '—')}`"
        )
    if run.get("template"):
        lines.append(f"- **Plantilla:** {run['template']} ({run.get('language', 'es')})")
    lines.append("")

    round_number = 1
    final_decision: dict | None = None

    for ev in run.get("events") or []:
        etype = ev.get("type")

        if etype == "run_started":
            lines.append(
                f"> Máximo {ev.get('max_rounds', '—')} rondas · "
                f"confianza requerida {round((ev.get('confidence_threshold') or 0) * 100)}%\n"
            )
            lines.append(f"## Ronda {round_number}\n")

        elif etype == "agent_completed":
            lines.append(f"### {ev.get('role', ev.get('node', ''))}\n")
            lines.append(str(ev.get("content", "")).strip() + "\n")

        elif etype == "tool_call":
            status = "ERROR" if ev.get("error") else ev.get("approval", "auto")
            lines.append(
                f"#### Herramienta: `{ev.get('tool_name')}` "
                f"({ev.get('agent_role', '')}, {status})\n"
            )
            lines.append("```json")
            lines.append(json.dumps(ev.get("args") or {}, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("```")
            lines.append(str(ev.get("result", "")).strip())
            lines.append("```\n")

        elif etype == "user_comment" and ev.get("content"):
            lines.append("### Comentario del operador\n")
            lines.append(str(ev["content"]).strip() + "\n")

        elif etype == "moderator_decision":
            d = ev.get("decision") or {}
            final_decision = d
            lines.append("### Decisión del moderador\n")
            lines.append(f"- **Estado:** {d.get('status', '—')}")
            lines.append(f"- **Confianza:** {round((d.get('confidence') or 0) * 100)}%")
            lines.append(f"- **Riesgo:** {d.get('risk_level', '—')}")
            if d.get("leading_hypothesis"):
                lines.append(f"- **Hipótesis principal:** {d['leading_hypothesis']}")
            if d.get("next_step"):
                lines.append(f"- **Siguiente paso:** {d['next_step']}")
            lines.append("")
            if d.get("evidence"):
                lines.append("**Evidencia:**\n")
                lines.append(_fmt_list(d["evidence"]))
            if d.get("missing_evidence"):
                lines.append("**Evidencia faltante:**\n")
                lines.append(_fmt_list(d["missing_evidence"]))
            if d.get("rejected_hypotheses"):
                lines.append("**Hipótesis rechazadas:**\n")
                lines.append(_fmt_list(d["rejected_hypotheses"]))
            if d.get("recommended_fix"):
                lines.append(f"**Fix recomendado:** {d['recommended_fix']}\n")
            if d.get("validation"):
                lines.append("**Validación:**\n")
                lines.append(_fmt_list(d["validation"]))
            if d.get("stop_reason"):
                lines.append(f"**Motivo de cierre:** {d['stop_reason']}\n")
            if d.get("status") == "continue":
                round_number = ev.get("round") or (round_number + 1)
                lines.append(f"## Ronda {round_number}\n")

        elif etype == "error":
            lines.append(f"### Error\n\n{ev.get('message', '')}\n")

        elif etype == "run_cancelled":
            lines.append("### Debate detenido manualmente\n")

    if final_decision:
        lines.append("---\n")
        lines.append("## Resumen ejecutivo\n")
        lines.append(f"- **Estado final:** {final_decision.get('status', '—')}")
        lines.append(f"- **Confianza:** {round((final_decision.get('confidence') or 0) * 100)}%")
        lines.append(f"- **Riesgo:** {final_decision.get('risk_level', '—')}")
        if final_decision.get("leading_hypothesis"):
            lines.append(f"- **Hipótesis:** {final_decision['leading_hypothesis']}")
        if final_decision.get("recommended_fix"):
            lines.append(f"- **Fix recomendado:** {final_decision['recommended_fix']}")
        if final_decision.get("next_step"):
            lines.append(f"- **Siguiente paso:** {final_decision['next_step']}")
        lines.append("")

    return "\n".join(lines)
