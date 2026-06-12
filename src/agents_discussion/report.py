"""Markdown report generation from a stored run record."""
from __future__ import annotations

import json
import re

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


def _fmt_duration(secs: float | None) -> str:
    if secs is None or secs < 0:
        return "—"
    s = round(secs)
    if s < 60:
        return f"{s}s"
    m, rs = divmod(s, 60)
    if m < 60:
        return f"{m}m {rs}s" if rs else f"{m}m"
    h, rm = divmod(m, 60)
    return f"{h}h {rm}m" if rm else f"{h}h"


def build_markdown_report(run: dict) -> str:
    """Render a complete debate run as a self-contained Markdown report."""
    lines: list[str] = []
    topic = run.get("topic", "")
    models = run.get("models") or {}
    efforts = run.get("reasoning_effort") or {}

    lines.append(f"# Informe de diagnóstico: {topic}\n")
    lines.append(f"- **Run ID:** `{run.get('run_id', '')}`")
    lines.append(f"- **Inicio:** {run.get('timestamp', '—')}")
    if run.get("finished_at"):
        lines.append(f"- **Fin:** {run['finished_at']}")
    if run.get("duration_seconds") is not None:
        lines.append(f"- **Duración:** {_fmt_duration(run['duration_seconds'])}")
    lines.append(f"- **Estado:** {_STATUS_LABELS.get(run.get('status'), run.get('status', '—'))}")
    if run.get("parent_run_id"):
        lines.append(f"- **Reanudado desde:** `{run['parent_run_id']}`")
    if models:
        lines.append(
            f"- **Modelos:** diagnóstico `{models.get('diagnostic', '—')}` · "
            f"escéptico `{models.get('skeptic', '—')}` · moderador `{models.get('moderator', '—')}`"
        )
    if efforts and any(v and v != "none" for v in efforts.values()):
        def _eff(key: str) -> str:
            v = efforts.get(key) or "none"
            return v if v != "none" else "—"
        lines.append(
            f"- **Nivel de thinking:** diagnóstico `{_eff('diagnostic')}` · "
            f"escéptico `{_eff('skeptic')}` · moderador `{_eff('moderator')}`"
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

        elif etype == "agent_skipped":
            lines.append(
                f"### {ev.get('role', ev.get('node', ''))} — **OMITIDO**\n"
            )
            lines.append(f"*Razón:* {ev.get('rationale', 'Decisión del moderador')}\n")

        elif etype == "history_compressed":
            lines.append(
                f"> 📦 *Resumen de rondas anteriores generado (ronda {ev.get('round', '—')}).*\n"
            )

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
            fd = d.get("flow_directive")
            if fd:
                lines.append(
                    f"- **Flujo próxima ronda:** "
                    f"skip_skeptic={fd.get('skip_skeptic', False)}, "
                    f"skip_rebuttal={fd.get('skip_rebuttal', False)}"
                )
                if fd.get("rationale"):
                    lines.append(f"  - *Razón:* {fd['rationale']}")
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

    # Infer hypotheses from diagnostic agent completions if available
    hypotheses: list[dict] = []
    for ev in run.get("events") or []:
        if ev.get("type") == "agent_completed" and ev.get("node") == "diagnostic_agent":
            content = str(ev.get("content", ""))
            # Extract HYPOTHESIS blocks
            matches = re.finditer(
                r"###\s*HYPOTHESIS-([A-Za-z0-9_-]+)\s*\n\s*Text:\s*(.+?)(?=\n###|\n##|\Z)",
                content,
                re.IGNORECASE | re.DOTALL,
            )
            for m in matches:
                hypotheses.append({"id": m.group(1).strip(), "text": m.group(2).strip()})

    if final_decision:
        lines.append("---\n")
        lines.append("## Resumen ejecutivo\n")
        lines.append(f"- **Estado final:** {final_decision.get('status', '—')}")
        lines.append(f"- **Confianza:** {round((final_decision.get('confidence') or 0) * 100)}%")
        lines.append(f"- **Riesgo:** {final_decision.get('risk_level', '—')}")
        if hypotheses:
            lines.append("- **Hipótesis detectadas:**")
            for hyp in hypotheses:
                lines.append(f"  - `{hyp['id']}`: {hyp['text'][:120]}...")
        if final_decision.get("leading_hypothesis"):
            lines.append(f"- **Hipótesis principal:** {final_decision['leading_hypothesis']}")
        if final_decision.get("recommended_fix"):
            lines.append(f"- **Fix recomendado:** {final_decision['recommended_fix']}")
        if final_decision.get("next_step"):
            lines.append(f"- **Siguiente paso:** {final_decision['next_step']}")
        lines.append("")

    # Token consumption section
    lines.extend(_token_section(run.get("token_totals"), run.get("cost_estimate")))

    return "\n".join(lines)


_AGENT_LABELS = {
    "diagnostic_agent":          "Diagnóstico Principal",
    "skeptic_agent":             "Revisor Escéptico",
    "diagnostic_rebuttal_agent": "Contrarréplica",
    "moderator_agent":           "Moderador",
    "summarize_history":         "Resumen de historia",
}


def _fmt_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _token_section(token_totals: dict | None, cost_estimate: dict | None) -> list[str]:
    """Build a Markdown section summarising token consumption and estimated cost."""
    if not token_totals:
        return []
    lines: list[str] = ["---\n", "## Consumo de tokens\n"]
    by_node = token_totals.get("by_node") or {}
    total   = token_totals.get("total") or {}
    by_node_cost = (cost_estimate or {}).get("by_node") or {}
    total_usd    = (cost_estimate or {}).get("total_usd")

    # Header row
    lines.append("| Agente | Entrada | Salida | Total | Coste est. (USD) |")
    lines.append("|---|---|---|---|---|")
    for node, counts in by_node.items():
        label = _AGENT_LABELS.get(node, node)
        nc = by_node_cost.get(node, {})
        usd = nc.get("estimated_usd") if nc else None
        usd_str = f"${usd:.4f}" if usd is not None else "—"
        lines.append(
            f"| {label} "
            f"| {_fmt_tokens(counts.get('input_tokens'))} "
            f"| {_fmt_tokens(counts.get('output_tokens'))} "
            f"| {_fmt_tokens(counts.get('total_tokens'))} "
            f"| {usd_str} |"
        )
    # Totals row
    total_usd_str = f"${total_usd:.4f}" if total_usd is not None else "—"
    lines.append(
        f"| **TOTAL** "
        f"| **{_fmt_tokens(total.get('input_tokens'))}** "
        f"| **{_fmt_tokens(total.get('output_tokens'))}** "
        f"| **{_fmt_tokens(total.get('total_tokens'))}** "
        f"| **{total_usd_str}** |"
    )
    lines.append("")
    if cost_estimate and not cost_estimate.get("has_prices"):
        lines.append(
            "> Precio no disponible para uno o más modelos. "
            "Configura `MODEL_PRICES_FILE` para estimaciones precisas.\n"
        )
    return lines
