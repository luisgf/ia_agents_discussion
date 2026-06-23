# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

"""Markdown report generation from a stored run record."""

from __future__ import annotations

import json
import re

_STATUS_LABELS = {
    "running": "Running",
    "completed": "Completed",
    "cancelled": "Stopped",
    "error": "Error",
    "interrupted": "Interrupted",
}


def _fmt_list(items: list | None) -> str:
    if not items:
        return "- (none)\n"
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

    lines.append(f"# Diagnosis report: {topic}\n")
    lines.append(f"- **Run ID:** `{run.get('run_id', '')}`")
    lines.append(f"- **Start:** {run.get('timestamp', '—')}")
    if run.get("finished_at"):
        lines.append(f"- **End:** {run['finished_at']}")
    if run.get("duration_seconds") is not None:
        lines.append(f"- **Duration:** {_fmt_duration(run['duration_seconds'])}")
    lines.append(f"- **Status:** {_STATUS_LABELS.get(run.get('status'), run.get('status', '—'))}")
    if run.get("parent_run_id"):
        lines.append(f"- **Resumed from:** `{run['parent_run_id']}`")
    if models:
        lines.append(
            f"- **Models:** diagnosis `{models.get('diagnostic', '—')}` · "
            f"skeptic `{models.get('skeptic', '—')}` · moderator `{models.get('moderator', '—')}`"
        )
    if efforts and any(v and v != "none" for v in efforts.values()):

        def _eff(key: str) -> str:
            v = efforts.get(key) or "none"
            return v if v != "none" else "—"

        lines.append(
            f"- **Thinking level:** diagnosis `{_eff('diagnostic')}` · "
            f"skeptic `{_eff('skeptic')}` · moderator `{_eff('moderator')}`"
        )
    if run.get("template"):
        lines.append(f"- **Template:** {run['template']} ({run.get('language', 'es')})")
    lines.append("")

    round_number = 1
    final_decision: dict | None = None

    for ev in run.get("events") or []:
        etype = ev.get("type")

        if etype == "run_started":
            lines.append(
                f"> Maximum {ev.get('max_rounds', '—')} rounds · "
                f"required confidence {round((ev.get('confidence_threshold') or 0) * 100)}%\n"
            )
            lines.append(f"## Round {round_number}\n")

        elif etype == "agent_completed":
            lines.append(f"### {ev.get('role', ev.get('node', ''))}\n")
            lines.append(str(ev.get("content", "")).strip() + "\n")

        elif etype == "agent_skipped":
            lines.append(f"### {ev.get('role', ev.get('node', ''))} — **SKIPPED**\n")
            lines.append(f"*Reason:* {ev.get('rationale', 'Moderator decision')}\n")

        elif etype == "history_compressed":
            lines.append(f"> 📦 *Summary of previous rounds generated (round {ev.get('round', '—')}).*\n")

        elif etype == "tool_call":
            status = "ERROR" if ev.get("error") else ev.get("approval", "auto")
            lines.append(f"#### Tool: `{ev.get('tool_name')}` ({ev.get('agent_role', '')}, {status})\n")
            lines.append("```json")
            lines.append(json.dumps(ev.get("args") or {}, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("```")
            lines.append(str(ev.get("result", "")).strip())
            lines.append("```\n")

        elif etype == "user_comment" and ev.get("content"):
            lines.append("### Operator comment\n")
            lines.append(str(ev["content"]).strip() + "\n")

        elif etype == "moderator_decision":
            d = ev.get("decision") or {}
            final_decision = d
            lines.append("### Moderator decision\n")
            lines.append(f"- **Status:** {d.get('status', '—')}")
            lines.append(f"- **Confidence:** {round((d.get('confidence') or 0) * 100)}%")
            lines.append(f"- **Risk:** {d.get('risk_level', '—')}")
            fd = d.get("flow_directive")
            if fd:
                lines.append(
                    f"- **Next round flow:** "
                    f"skip_skeptic={fd.get('skip_skeptic', False)}, "
                    f"skip_rebuttal={fd.get('skip_rebuttal', False)}"
                )
                if fd.get("rationale"):
                    lines.append(f"  - *Reason:* {fd['rationale']}")
            if d.get("leading_hypothesis"):
                lines.append(f"- **Leading hypothesis:** {d['leading_hypothesis']}")
            if d.get("next_step"):
                lines.append(f"- **Next step:** {d['next_step']}")
            lines.append("")
            if d.get("evidence"):
                lines.append("**Evidence:**\n")
                lines.append(_fmt_list(d["evidence"]))
            if d.get("missing_evidence"):
                lines.append("**Missing evidence:**\n")
                lines.append(_fmt_list(d["missing_evidence"]))
            if d.get("rejected_hypotheses"):
                lines.append("**Rejected hypotheses:**\n")
                lines.append(_fmt_list(d["rejected_hypotheses"]))
            if d.get("recommended_fix"):
                lines.append(f"**Recommended fix:** {d['recommended_fix']}\n")
            if d.get("validation"):
                lines.append("**Validation:**\n")
                lines.append(_fmt_list(d["validation"]))
            if d.get("stop_reason"):
                lines.append(f"**Closure reason:** {d['stop_reason']}\n")
            if d.get("status") == "continue":
                round_number = ev.get("round") or (round_number + 1)
                lines.append(f"## Round {round_number}\n")

        elif etype == "error":
            lines.append(f"### Error\n\n{ev.get('message', '')}\n")

        elif etype == "run_cancelled":
            lines.append("### Debate stopped manually\n")

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
        lines.append("## Executive summary\n")
        lines.append(f"- **Final status:** {final_decision.get('status', '—')}")
        lines.append(f"- **Confidence:** {round((final_decision.get('confidence') or 0) * 100)}%")
        lines.append(f"- **Risk:** {final_decision.get('risk_level', '—')}")
        if hypotheses:
            lines.append("- **Detected hypotheses:**")
            for hyp in hypotheses:
                lines.append(f"  - `{hyp['id']}`: {hyp['text'][:120]}...")
        if final_decision.get("leading_hypothesis"):
            lines.append(f"- **Leading hypothesis:** {final_decision['leading_hypothesis']}")
        if final_decision.get("recommended_fix"):
            lines.append(f"- **Recommended fix:** {final_decision['recommended_fix']}")
        if final_decision.get("next_step"):
            lines.append(f"- **Next step:** {final_decision['next_step']}")
        lines.append("")

    # Token consumption section
    lines.extend(_token_section(run.get("token_totals"), run.get("cost_estimate")))

    return "\n".join(lines)


_AGENT_LABELS = {
    "diagnostic_agent": "Primary Diagnosis",
    "skeptic_agent": "Skeptical Reviewer",
    "diagnostic_rebuttal_agent": "Rebuttal",
    "moderator_agent": "Moderator",
    "summarize_history": "History summary",
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
    lines: list[str] = ["---\n", "## Token consumption\n"]
    by_node = token_totals.get("by_node") or {}
    total = token_totals.get("total") or {}
    by_node_cost = (cost_estimate or {}).get("by_node") or {}
    total_usd = (cost_estimate or {}).get("total_usd")

    # Header row
    lines.append("| Agent | Input | Output | Total | Est. cost (USD) |")
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
        lines.append("> Price unavailable for one or more models. Set `MODEL_PRICES_FILE` for accurate estimates.\n")
    return lines
