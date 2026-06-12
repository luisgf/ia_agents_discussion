"""Per-turn prompt scaffolding (user messages) in es/en.

System prompts live in versioned templates (see prompt_store.py). This module
builds the per-turn user messages and the history formatting.
"""

_L = {
    "es": {
        "topic": "Tema técnico:",
        "context": "Contexto disponible:",
        "no_context": "No se proporcionó contexto adicional.",
        "round": "Ronda:",
        "history": "Historial:",
        "no_history": "Sin historial previo.",
        "history_summary": "Resumen de rondas anteriores:",
        "last_round": "Mensajes recientes (no incluidos en el resumen):",
        "hypotheses": "Hipótesis en debate:",
        "diag_deliver": """Entrega:
1. Hipótesis principal y alternativas. Cada hipótesis debe comenzar con:
   ### HYPOTHESIS-<n> [P=<0-1>]
   Text: <descripción concisa>
   P es tu probabilidad estimada con la evidencia ACTUAL (ej. [P=0.6]). Si la evidencia no es
   concluyente, propón AL MENOS 2 hipótesis competitivas, ordenadas por P descendente.
   Recalibra la P de las hipótesis existentes en cada ronda según la evidencia nueva.
   Esto permite rastrearlas estructuradamente. Puedes citarlas luego como [hypothesis:<id>].
   Si una hipótesis ya aparece en "Hipótesis en debate", reutiliza su id EXACTO (no la renumeres);
   las hipótesis nuevas continúan la numeración existente, nunca reinicies en 1.
2. Observaciones que las soportan: inclúyelas como líneas dentro del bloque de cada hipótesis,
   citando salidas de herramientas con [tool:<nombre>].
3. Inferencias y supuestos.
4. Experimento mínimo para confirmar o descartar — ejecútalo con la herramienta adecuada y reporta la salida real ([tool:<nombre>]).
5. Fix mínimo reversible si aplica. Si decides ejecutarlo, usa run_local_command o run_ssh_command (requerirá aprobación).
6. Si la evidencia es contundente y no hay ambigüedad relevante, añade al final:
   EARLY_OUT_RECOMMENDED: true
   EARLY_OUT_CONFIDENCE: 0.0-1.0
   EARLY_OUT_RATIONALE: <por qué>
7. Riesgos y validación.
Sé denso: máximo ~600 palabras; cita las salidas de tools resumidas (líneas clave), no las pegues íntegras.""",
        "skeptic_hypothesis": "Hipótesis del diagnóstico principal:",
        "skeptic_deliver": """Para cada hipótesis listada (por su id [hypothesis:<id>]), entrega:
1. Estado: accepted | rejected | needs_evidence, y tu probabilidad recalibrada
   tras la revisión como [P=<0-1>] junto al id.
2. Justificación técnica.
3. Causas alternativas plausibles.
4. Evidencia faltante crítica.
5. Riesgos del experimento o fix.
6. Prueba concreta que resolvería cada objeción — ejecútala con la herramienta adecuada y reporta la salida ([tool:<nombre>]).
Sé denso: máximo ~600 palabras; cita las salidas de tools resumidas (líneas clave), no las pegues íntegras.""",
        "rebuttal_initial": "Tu hipótesis inicial:",
        "rebuttal_critique": "Crítica escéptica:",
        "rebuttal_deliver": """Entrega una contrarréplica técnica:
1. Qué críticas aceptas y qué hipótesis actualizas (por id).
2. Qué hipótesis descartas y por qué.
3. Hipótesis principal actualizada.
4. Experimento o fix mínimo actualizado.
5. Validación y rollback.
Sé denso: máximo ~600 palabras; cita las salidas de tools resumidas (líneas clave), no las pegues íntegras.""",
        "mod_round": "Ronda actual:",
        "mod_max_rounds": "Máximo de rondas:",
        "mod_threshold": "Umbral de confianza para cerrar:",
        "mod_early": "Umbral para early-out (evidencia contundente):",
        "mod_history": "Historial del debate:",
        "mod_diag": "Diagnóstico principal (ronda actual):",
        "mod_skeptic": "Crítica escéptica (ronda actual):",
        "mod_rebuttal": "Contrarréplica (ronda actual):",
        "mod_decide": (
            "Decide si continuar o cerrar. Si continúas, también decide el flujo de la próxima ronda:\n"
            "- Si la evidencia del diagnóstico es ya contundente, puedes saltar al escéptico (skip_skeptic=true).\n"
            "- Si la respuesta del escéptico no aporta nada nuevo, puedes saltar la contrarréplica (skip_rebuttal=true).\n"
            "Incluye flow_directive con skip_skeptic (bool), skip_rebuttal (bool) y rationale.\n"
            "Si cierras, explica el motivo en stop_reason."
        ),
        "json_only": (
            "Responde ÚNICAMENTE con un objeto JSON válido que siga este esquema "
            "(sin texto adicional antes ni después, y sin fences de código ```):"
        ),
    },
    "en": {
        "topic": "Technical topic:",
        "context": "Available context:",
        "no_context": "No additional context was provided.",
        "round": "Round:",
        "history": "History:",
        "no_history": "No previous history.",
        "history_summary": "Summary of previous rounds:",
        "last_round": "Recent messages (not covered by the summary):",
        "hypotheses": "Hypotheses under debate:",
        "diag_deliver": """Deliver:
1. Leading hypothesis and alternatives. Each hypothesis must start with:
   ### HYPOTHESIS-<n> [P=<0-1>]
   Text: <concise description>
   P is your estimated probability given the CURRENT evidence (e.g. [P=0.6]). If the evidence is
   not conclusive, propose AT LEAST 2 competing hypotheses, ordered by descending P.
   Recalibrate the P of existing hypotheses each round as new evidence arrives.
   This enables structured tracking. You may reference them later as [hypothesis:<id>].
   If a hypothesis already appears under "Hypotheses under debate", reuse its EXACT id (do not renumber);
   new hypotheses continue the existing numbering, never restart at 1.
2. Supporting observations: include them as lines inside each hypothesis block,
   citing tool outputs with [tool:<name>].
3. Inferences and assumptions.
4. Minimal experiment to confirm or discard — run it with the appropriate tool and report the real output ([tool:<name>]).
5. Minimal reversible fix if applicable. If you execute it, use run_local_command or run_ssh_command (operator approval required).
6. If the evidence is conclusive and there is no relevant ambiguity, add at the end:
   EARLY_OUT_RECOMMENDED: true
   EARLY_OUT_CONFIDENCE: 0.0-1.0
   EARLY_OUT_RATIONALE: <why>
7. Risks and validation.
Be dense: ~600 words max; quote tool outputs summarized (key lines only), never paste them verbatim.""",
        "skeptic_hypothesis": "Leading diagnostic hypotheses:",
        "skeptic_deliver": """For each listed hypothesis (by its id [hypothesis:<id>]), deliver:
1. State: accepted | rejected | needs_evidence, plus your recalibrated probability
   after review as [P=<0-1>] next to the id.
2. Technical justification.
3. Plausible alternative causes.
4. Critical missing evidence.
5. Risks of the experiment or fix.
6. Concrete test that would resolve each objection — run it with the appropriate tool and report the output ([tool:<name>]).
Be dense: ~600 words max; quote tool outputs summarized (key lines only), never paste them verbatim.""",
        "rebuttal_initial": "Your initial hypothesis:",
        "rebuttal_critique": "Skeptical critique:",
        "rebuttal_deliver": """Deliver a technical rebuttal:
1. Which critiques you accept and which hypotheses you update (by id).
2. Which hypotheses you discard and why.
3. Updated leading hypothesis.
4. Updated minimal experiment or fix.
5. Validation and rollback.
Be dense: ~600 words max; quote tool outputs summarized (key lines only), never paste them verbatim.""",
        "mod_round": "Current round:",
        "mod_max_rounds": "Maximum rounds:",
        "mod_threshold": "Confidence threshold to close:",
        "mod_early": "Early-out threshold (conclusive evidence):",
        "mod_history": "Debate history:",
        "mod_diag": "Leading diagnosis (current round):",
        "mod_skeptic": "Skeptical critique (current round):",
        "mod_rebuttal": "Rebuttal (current round):",
        "mod_decide": (
            "Decide whether to continue or close. If you continue, also decide the next round's flow:\n"
            "- If the diagnostic evidence is already conclusive, you may skip the skeptic (skip_skeptic=true).\n"
            "- If the skeptic's response adds nothing new, you may skip the rebuttal (skip_rebuttal=true).\n"
            "Include flow_directive with skip_skeptic (bool), skip_rebuttal (bool) and rationale.\n"
            "If you close, explain why in stop_reason."
        ),
        "json_only": (
            "Respond ONLY with a valid JSON object following this schema "
            "(no additional text before or after, and no code fences ```):"
        ),
    },
}

FLOW_SCHEMA = """  "flow_directive": {
    "skip_skeptic": <true|false>,
    "skip_rebuttal": <true|false>,
    "rationale": "<why this flow was chosen>"
  }"""

MODERATOR_JSON_SCHEMA = """{
  "status": "<continue|final_diagnosis|needs_more_data|propose_fix|structured_uncertainty>",
  "confidence": <0.0-1.0>,
  "leading_hypothesis": "<most likely technical cause or empty>",
  "evidence": ["<concrete observation>"],
  "missing_evidence": ["<data required to move forward>"],
  "rejected_hypotheses": ["<discarded alternative>"],
  "next_step": "<cheapest safe diagnostic or remediation step>",
  "recommended_fix": "<minimal reversible fix or null>",
  "risk_level": "<low|medium|high>",
  "validation": ["<how to verify the diagnosis or fix>"],
  "stop_reason": "<closure reason or null>",
""" + FLOW_SCHEMA + """
}"""


def _labels(language: str) -> dict:
    return _L.get(language) or _L["es"]


def format_history(
    history: list[object],
    language: str = "es",
    history_summary: str = "",
    mode: str = "full",
) -> str:
    if mode == "compressed" and history_summary:
        # The summary covers finished rounds; `history` carries only the recent
        # tail the caller wants verbatim (e.g. HITL comments of the new round).
        lines = [
            _labels(language)["history_summary"],
            history_summary,
        ]
        if history:
            lines.append("")
            lines.append(_labels(language)["last_round"])
            lines.append(
                "\n\n".join(f"[{item.role}]\n{item.content}" for item in history)
            )
        return "\n".join(lines)
    if not history:
        return _labels(language)["no_history"]
    return "\n\n".join(f"[{item.role}]\n{item.content}" for item in history)


def _format_hypotheses(hypotheses: list[object], language: str = "es") -> str:
    if not hypotheses:
        return "- Ninguna."
    lines = []
    for h in hypotheses:
        status = f"[{h.state}]" if hasattr(h, "state") else ""
        probability = getattr(h, "probability", None)
        prob = f" [P={probability:.2f}]" if probability is not None else ""
        lines.append(f"- {h.id} {status}{prob}: {h.text}")
        if getattr(h, "supporting_evidence", None):
            for ev in h.supporting_evidence:
                lines.append(f"    · {ev}")
        if getattr(h, "rejected_reason", None):
            lines.append(f"    · Rejected because: {h.rejected_reason}")
    return "\n".join(lines)


def diagnostic_prompt(
    topic: str,
    context: str,
    round_number: int,
    history: list[object],
    hypotheses: list[object] | None = None,
    language: str = "es",
    history_summary: str = "",
    mode: str = "full",
) -> str:
    t = _labels(language)
    return f"""
{t["topic"]}
{topic}

{t["context"]}
{context or t["no_context"]}

{t["round"]} {round_number}

{t["hypotheses"]}
{_format_hypotheses(hypotheses or [], language)}

{t["history"]}
{format_history(history, language, history_summary, mode=mode)}

{t["diag_deliver"]}
""".strip()


def skeptic_prompt(
    topic: str,
    context: str,
    diagnostic_response: str,
    hypotheses: list[object],
    history: list[object],
    language: str = "es",
    history_summary: str = "",
    mode: str = "full",
) -> str:
    t = _labels(language)
    return f"""
{t["topic"]}
{topic}

{t["context"]}
{context or t["no_context"]}

{t["skeptic_hypothesis"]}
{diagnostic_response}

{t["hypotheses"]}
{_format_hypotheses(hypotheses, language)}

{t["history"]}
{format_history(history, language, history_summary, mode=mode)}

{t["skeptic_deliver"]}
""".strip()


def rebuttal_prompt(
    topic: str,
    context: str,
    diagnostic_response: str,
    skeptic_response: str,
    hypotheses: list[object],
    history: list[object] | None = None,
    language: str = "es",
    history_summary: str = "",
    mode: str = "full",
) -> str:
    t = _labels(language)
    return f"""
{t["topic"]}
{topic}

{t["context"]}
{context or t["no_context"]}

{t["rebuttal_initial"]}
{diagnostic_response}

{t["rebuttal_critique"]}
{skeptic_response}

{t["hypotheses"]}
{_format_hypotheses(hypotheses, language)}

{t["history"]}
{format_history(history or [], language, history_summary, mode=mode)}

{t["rebuttal_deliver"]}
""".strip()


def moderator_prompt(
    topic: str,
    context: str,
    round_number: int,
    max_rounds: int,
    confidence_threshold: float,
    early_out_threshold: float,
    diagnostic_response: str,
    skeptic_response: str,
    diagnostic_rebuttal: str,
    hypotheses: list[object],
    history: list[object] | None = None,
    history_summary: str = "",
    language: str = "es",
    mode: str = "full",
) -> str:
    t = _labels(language)
    return f"""
{t["topic"]}
{topic}

{t["context"]}
{context or t["no_context"]}

{t["mod_round"]} {round_number}
{t["mod_max_rounds"]} {max_rounds}
{t["mod_threshold"]} {confidence_threshold}
{t["mod_early"]} {early_out_threshold}

{t["hypotheses"]}
{_format_hypotheses(hypotheses, language)}

{t["mod_history"]}
{format_history(history or [], language, history_summary, mode=mode)}

{t["mod_diag"]}
{diagnostic_response}

{t["mod_skeptic"]}
{skeptic_response}

{t["mod_rebuttal"]}
{diagnostic_rebuttal}

{t["mod_decide"]}

{t["json_only"]}

{MODERATOR_JSON_SCHEMA}
""".strip()


def moderator_json_fallback_suffix(language: str = "es") -> str:
    """Appended to the moderator prompt only when the model/endpoint
    does not support structured output natively. Since the base prompt
    already includes the JSON schema, this suffix is now a thin reminder."""
    t = _labels(language)
    return f"\n\n{t['json_only']}"
