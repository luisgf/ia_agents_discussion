# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

from agents_discussion.graph import _extract_hypotheses, _parse_probability, skeptic_agent
from agents_discussion.prompts import _format_hypotheses
from agents_discussion.state import Hypothesis, HypothesisTransition, _merge_hypotheses


def _hyp(hyp_id: str = "1", **kwargs) -> Hypothesis:
    defaults = {
        "id": hyp_id,
        "text": "Hipótesis de prueba",
        "state": "active",
        "proposer": "diagnostic_agent",
        "round": 1,
    }
    defaults.update(kwargs)
    return Hypothesis(**defaults)


# ── _parse_probability ──────────────────────────────────────────────────


def test_parse_probability_basic() -> None:
    assert _parse_probability("0.6") == 0.6
    assert _parse_probability(".75") == 0.75


def test_parse_probability_clamps() -> None:
    assert _parse_probability("1.5") == 1.0
    assert _parse_probability("0") == 0.0


def test_parse_probability_absent_or_invalid() -> None:
    assert _parse_probability(None) is None
    assert _parse_probability("") is None
    assert _parse_probability("..") is None


# ── _extract_hypotheses con [P=...] ─────────────────────────────────────


def test_extract_with_probability() -> None:
    text = "### HYPOTHESIS-1 [P=0.6]\nText: Falta un índice.\n"
    hyps = _extract_hypotheses(text, "diagnostic_agent", 1)
    assert len(hyps) == 1
    assert hyps[0].probability == 0.6


def test_extract_probability_with_spaces() -> None:
    text = "### HYPOTHESIS-2 [ P = 0.75 ]\nText: Pool saturado.\n"
    hyps = _extract_hypotheses(text, "diagnostic_agent", 1)
    assert hyps[0].probability == 0.75


def test_extract_without_probability_backcompat() -> None:
    text = "### HYPOTHESIS-1\nText: Falta un índice.\n"
    hyps = _extract_hypotheses(text, "diagnostic_agent", 1)
    assert hyps[0].probability is None
    assert hyps[0].text == "Falta un índice."


def test_extract_probability_out_of_range_clamped() -> None:
    text = "### HYPOTHESIS-1 [P=3.0]\nText: X.\n"
    hyps = _extract_hypotheses(text, "diagnostic_agent", 1)
    assert hyps[0].probability == 1.0


def test_extract_multiple_blocks_mixed() -> None:
    text = (
        "### HYPOTHESIS-1 [P=0.7]\nText: Causa A.\n\n"
        "- [tool:run_ssh_command] evidencia A\n\n"
        "### HYPOTHESIS-2\nText: Causa B.\n"
    )
    hyps = _extract_hypotheses(text, "diagnostic_agent", 2)
    assert [h.id for h in hyps] == ["1", "2"]
    assert hyps[0].probability == 0.7
    assert hyps[0].supporting_evidence == ["[tool:run_ssh_command] evidencia A"]
    assert hyps[1].probability is None


def test_fallback_has_no_probability() -> None:
    hyps = _extract_hypotheses("Sin formato estructurado.", "diagnostic_agent", 3)
    assert hyps[0].id == "R3-F1"
    assert hyps[0].probability is None


# ── _merge_hypotheses con probability ───────────────────────────────────


def test_merge_incoming_probability_wins() -> None:
    merged = _merge_hypotheses([_hyp(probability=0.4)], [_hyp(probability=0.9)])
    assert merged[0].probability == 0.9


def test_merge_incoming_none_preserves_existing() -> None:
    merged = _merge_hypotheses([_hyp(probability=0.4)], [_hyp(probability=None)])
    assert merged[0].probability == 0.4


# ── _format_hypotheses ──────────────────────────────────────────────────


def test_format_shows_probability() -> None:
    out = _format_hypotheses([_hyp(probability=0.65)])
    assert "[P=0.65]" in out


def test_format_omits_probability_when_none() -> None:
    out = _format_hypotheses([_hyp()])
    assert "[P=" not in out


# ── Recalibración del escéptico (estado + P desde snippet) ─────────────


def _run_skeptic(monkeypatch, content: str, hypotheses: list[Hypothesis]) -> dict:
    import agents_discussion.graph as g

    monkeypatch.setattr(g, "_should_skip", lambda state, node: False)
    monkeypatch.setattr(g, "_resolve_effort", lambda *a, **k: None)
    monkeypatch.setattr(g, "_run_with_tools", lambda *a, **k: (content, [], {}))
    monkeypatch.setattr(g, "_template_for", lambda state: type("T", (), {"skeptic_system": "sys"})())
    state = {
        "topic": "t",
        "context": "",
        "diagnostic_response": "d",
        "history": [],
        "hypotheses": hypotheses,
        "round": 2,
        "skeptic_model": "m",
        "language": "es",
        "run_id": "",
        "compress_history": False,
        "history_summary": "",
    }
    return skeptic_agent(state)


def test_skeptic_extracts_state_and_probability(monkeypatch) -> None:
    content = "[hypothesis:1] rejected [P=0.1]\nReason: contradice las métricas.\n"
    result = _run_skeptic(monkeypatch, content, [_hyp(probability=0.7)])
    updated = result["hypotheses"]
    assert len(updated) == 1
    assert updated[0].state == "rejected"
    assert updated[0].probability == 0.1
    assert updated[0].transitions[-1].to_state == "rejected"


def test_skeptic_probability_only_update(monkeypatch) -> None:
    content = "[hypothesis:1] needs_evidence [P=0.45], falta confirmar con métricas.\n"
    result = _run_skeptic(monkeypatch, content, [_hyp(probability=0.7)])
    updated = result["hypotheses"]
    assert len(updated) == 1
    assert updated[0].state == "active"
    assert updated[0].probability == 0.45
    # Sin cambio de estado no se añade transición
    assert all(t.agent != "skeptic_agent" for t in updated[0].transitions)


def test_skeptic_no_mention_no_update(monkeypatch) -> None:
    result = _run_skeptic(monkeypatch, "No menciono nada.", [_hyp()])
    assert result["hypotheses"] == []


def test_merge_transition_dedup_still_works() -> None:
    base = _hyp()
    rejected = base.model_copy(deep=True)
    rejected.state = "rejected"
    rejected.transitions = rejected.transitions + [
        HypothesisTransition(round=1, from_state="active", to_state="rejected", agent="skeptic_agent", note="x")
    ]
    merged = _merge_hypotheses([base], [rejected])
    merged = _merge_hypotheses(merged, [rejected])
    assert len(merged) == 1
    skeptic_transitions = [t for t in merged[0].transitions if t.agent == "skeptic_agent"]
    assert len(skeptic_transitions) == 1
