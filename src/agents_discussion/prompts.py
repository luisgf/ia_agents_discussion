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
        "diag_deliver": """Entrega:
1. Hipótesis principal.
2. Observaciones que la soportan (cita las salidas de herramientas con [tool:<nombre>]).
3. Inferencias y supuestos.
4. Experimento mínimo para confirmarla o descartarla — ejecútalo con la herramienta
   adecuada y reporta la salida real ([tool:<nombre>]); no lo describas solo como texto.
5. Fix mínimo reversible si aplica — si decides ejecutarlo, hazlo vía run_local_command
   o run_ssh_command (requerirá aprobación del operador).
6. Riesgos y validación.""",
        "skeptic_hypothesis": "Hipótesis/respuesta del diagnóstico principal:",
        "skeptic_deliver": """Entrega:
1. Debilidades de la hipótesis principal (señala afirmaciones sin evidencia instrumental).
2. Causas alternativas plausibles.
3. Evidencia faltante crítica.
4. Riesgos del experimento o fix.
5. Prueba concreta que resolvería cada objeción — ejecútala tú mismo con la herramienta
   adecuada y reporta la salida ([tool:<nombre>]); no la delegues como sugerencia.""",
        "rebuttal_initial": "Tu hipótesis inicial:",
        "rebuttal_critique": "Crítica escéptica:",
        "rebuttal_deliver": """Entrega una contrarréplica técnica:
1. Qué críticas aceptas.
2. Qué hipótesis ajustas o descartas.
3. Hipótesis principal actualizada.
4. Experimento o fix mínimo actualizado.
5. Validación y rollback.""",
        "mod_round": "Ronda actual:",
        "mod_max_rounds": "Máximo de rondas:",
        "mod_threshold": "Umbral de confianza para cerrar:",
        "mod_history": "Historial completo del debate (rondas anteriores incluidas):",
        "mod_diag": "Diagnóstico principal (ronda actual):",
        "mod_skeptic": "Crítica escéptica (ronda actual):",
        "mod_rebuttal": "Contrarréplica (ronda actual):",
        "mod_decide": (
            "Decide si continuar o cerrar. Si continúas, el next_step debe indicar el foco "
            "exacto de la siguiente ronda. Si cierras, explica el motivo en stop_reason."
        ),
        "json_only": (
            "Responde ÚNICAMENTE con un objeto JSON válido que siga este esquema "
            "(sin texto adicional antes ni después):"
        ),
    },
    "en": {
        "topic": "Technical topic:",
        "context": "Available context:",
        "no_context": "No additional context was provided.",
        "round": "Round:",
        "history": "History:",
        "no_history": "No previous history.",
        "diag_deliver": """Deliver:
1. Leading hypothesis.
2. Supporting observations (cite tool outputs with [tool:<name>]).
3. Inferences and assumptions.
4. Minimal experiment to confirm or discard it — run it with the appropriate tool
   and report the real output ([tool:<name>]); do not just describe it as text.
5. Minimal reversible fix if applicable — if you execute it, do so via
   run_local_command or run_ssh_command (operator approval required).
6. Risks and validation.""",
        "skeptic_hypothesis": "Leading diagnostic hypothesis/response:",
        "skeptic_deliver": """Deliver:
1. Weaknesses of the leading hypothesis (flag claims without instrumental evidence).
2. Plausible alternative causes.
3. Critical missing evidence.
4. Risks of the experiment or fix.
5. Concrete test that would resolve each objection — run it yourself with the
   appropriate tool and report the output ([tool:<name>]); do not defer it as
   a suggestion.""",
        "rebuttal_initial": "Your initial hypothesis:",
        "rebuttal_critique": "Skeptical critique:",
        "rebuttal_deliver": """Deliver a technical rebuttal:
1. Which critiques you accept.
2. Which hypotheses you adjust or discard.
3. Updated leading hypothesis.
4. Updated minimal experiment or fix.
5. Validation and rollback.""",
        "mod_round": "Current round:",
        "mod_max_rounds": "Maximum rounds:",
        "mod_threshold": "Confidence threshold to close:",
        "mod_history": "Full debate history (previous rounds included):",
        "mod_diag": "Leading diagnosis (current round):",
        "mod_skeptic": "Skeptical critique (current round):",
        "mod_rebuttal": "Rebuttal (current round):",
        "mod_decide": (
            "Decide whether to continue or close. If you continue, next_step must state the "
            "exact focus of the next round. If you close, explain why in stop_reason."
        ),
        "json_only": (
            "Respond ONLY with a valid JSON object following this schema "
            "(no additional text before or after):"
        ),
    },
}

MODERATOR_JSON_SCHEMA = """{
  "status": "<continue|final_diagnosis|needs_more_data|propose_fix|structured_uncertainty>",
  "confidence": <0.0-1.0>,
  "leading_hypothesis": "<causa técnica más probable o vacío>",
  "evidence": ["<observación concreta>"],
  "missing_evidence": ["<dato requerido para avanzar>"],
  "rejected_hypotheses": ["<alternativa descartada>"],
  "next_step": "<paso diagnóstico o de remediación más barato y seguro>",
  "recommended_fix": "<fix mínimo reversible o null>",
  "risk_level": "<low|medium|high>",
  "validation": ["<cómo verificar el diagnóstico o fix>"],
  "stop_reason": "<motivo de cierre o null>"
}"""


def _labels(language: str) -> dict:
    return _L.get(language) or _L["es"]


def format_history(history: list[object], language: str = "es") -> str:
    if not history:
        return _labels(language)["no_history"]
    return "\n\n".join(f"[{item.role}]\n{item.content}" for item in history)


def diagnostic_prompt(
    topic: str,
    context: str,
    round_number: int,
    history: list[object],
    language: str = "es",
) -> str:
    t = _labels(language)
    return f"""
{t["topic"]}
{topic}

{t["context"]}
{context or t["no_context"]}

{t["round"]} {round_number}

{t["history"]}
{format_history(history, language)}

{t["diag_deliver"]}
""".strip()


def skeptic_prompt(
    topic: str,
    context: str,
    diagnostic_response: str,
    history: list[object],
    language: str = "es",
) -> str:
    t = _labels(language)
    return f"""
{t["topic"]}
{topic}

{t["context"]}
{context or t["no_context"]}

{t["skeptic_hypothesis"]}
{diagnostic_response}

{t["history"]}
{format_history(history, language)}

{t["skeptic_deliver"]}
""".strip()


def rebuttal_prompt(
    topic: str,
    context: str,
    diagnostic_response: str,
    skeptic_response: str,
    language: str = "es",
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

{t["rebuttal_deliver"]}
""".strip()


def moderator_prompt(
    topic: str,
    context: str,
    round_number: int,
    max_rounds: int,
    confidence_threshold: float,
    diagnostic_response: str,
    skeptic_response: str,
    diagnostic_rebuttal: str,
    history: list[object] | None = None,
    language: str = "es",
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

{t["mod_history"]}
{format_history(history or [], language)}

{t["mod_diag"]}
{diagnostic_response}

{t["mod_skeptic"]}
{skeptic_response}

{t["mod_rebuttal"]}
{diagnostic_rebuttal}

{t["mod_decide"]}
""".strip()


def moderator_json_fallback_suffix(language: str = "es") -> str:
    """Appended to the moderator prompt only on the plain-text fallback path,
    when the model/endpoint does not support structured output."""
    t = _labels(language)
    return f"\n\n{t['json_only']}\n\n{MODERATOR_JSON_SCHEMA}"
