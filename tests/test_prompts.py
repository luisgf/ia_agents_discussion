import agents_discussion.graph as g
from agents_discussion.graph import (
    _history_before_current_round,
    _history_mode,
    _messages_after_last_moderator,
    _prompt_history,
    summarize_history,
)
from agents_discussion.prompts import format_history, moderator_prompt, skeptic_prompt
from agents_discussion.state import DebateMessage


def _msg(role: str, content: str) -> DebateMessage:
    return DebateMessage(role=role, content=content)


_ROUND1 = [
    _msg("diagnostic_agent", "diag R1"),
    _msg("skeptic_agent", "skeptic R1"),
    _msg("diagnostic_rebuttal", "rebuttal R1"),
    _msg("moderator", '{"status": "continue"}'),
]


# ── _history_before_current_round ───────────────────────────────────────


def test_round1_keeps_initial_history_drops_current_agents() -> None:
    history = [_msg("user", "contexto inicial del resume")] + _ROUND1[:3]
    out = _history_before_current_round(history, 1)
    assert [m.role for m in out] == ["user"]


def test_round2_cuts_at_first_moderator_keeps_user_comments() -> None:
    history = _ROUND1 + [_msg("user", "comentario HITL"), _msg("diagnostic_agent", "diag R2")]
    out = _history_before_current_round(history, 2)
    assert [m.role for m in out] == [
        "diagnostic_agent", "skeptic_agent", "diagnostic_rebuttal", "moderator", "user",
    ]
    assert out[-1].content == "comentario HITL"


def test_round1_empty_history() -> None:
    assert _history_before_current_round([], 1) == []


def test_rebuttal_role_literal_is_filtered() -> None:
    history = [_msg("diagnostic_rebuttal", "rebuttal en curso")]
    assert _history_before_current_round(history, 1) == []


def test_messages_after_last_moderator() -> None:
    history = _ROUND1 + [_msg("user", "c"), _msg("diagnostic_agent", "diag R2")]
    out = _messages_after_last_moderator(history)
    assert [m.role for m in out] == ["user", "diagnostic_agent"]
    assert _messages_after_last_moderator(_ROUND1[:2]) == _ROUND1[:2]


# ── _history_mode / _prompt_history ─────────────────────────────────────


def _state(**kwargs) -> dict:
    base = {"history": [], "history_summary": "", "round": 1, "compress_history": True}
    base.update(kwargs)
    return base


def test_history_mode_round1_full() -> None:
    assert _history_mode(_state(history_summary="resumen")) == ("", "full")


def test_history_mode_round2_with_summary_compressed() -> None:
    assert _history_mode(_state(round=2, history_summary="resumen")) == ("resumen", "compressed")


def test_history_mode_compress_disabled() -> None:
    assert _history_mode(_state(round=3, history_summary="resumen", compress_history=False)) == ("", "full")


def test_history_mode_no_summary_full() -> None:
    assert _history_mode(_state(round=3)) == ("", "full")


def test_prompt_history_compressed_keeps_only_user_tail_for_skeptic() -> None:
    history = _ROUND1 + [_msg("user", "comentario"), _msg("diagnostic_agent", "diag R2")]
    state = _state(history=history, round=2, history_summary="resumen")
    tail, summary, mode = _prompt_history(state, exclude_current_round=True)
    assert mode == "compressed"
    assert summary == "resumen"
    assert [m.role for m in tail] == ["user"]


def test_prompt_history_full_mode_for_diagnostic() -> None:
    history = list(_ROUND1)
    state = _state(history=history, round=2)  # sin summary → full
    out, summary, mode = _prompt_history(state, exclude_current_round=False)
    assert mode == "full"
    assert out == history


# ── No duplicación de respuestas en los prompts ─────────────────────────


def test_skeptic_prompt_contains_diagnostic_response_once() -> None:
    diag = "TEXTO-DIAGNOSTICO-UNICO"
    history = _ROUND1 + [_msg("diagnostic_agent", diag)]
    filtered = _history_before_current_round(history, 2)
    prompt = skeptic_prompt("topic", "", diag, [], filtered)
    assert prompt.count(diag) == 1


def test_moderator_prompt_contains_each_response_once() -> None:
    diag, skep, reb = "DIAG-R2-X", "SKEP-R2-Y", "REB-R2-Z"
    history = _ROUND1 + [
        _msg("diagnostic_agent", diag), _msg("skeptic_agent", skep), _msg("diagnostic_rebuttal", reb),
    ]
    filtered = _history_before_current_round(history, 2)
    prompt = moderator_prompt("topic", "", 2, 4, 0.8, 0.9, diag, skep, reb, [], history=filtered)
    assert prompt.count(diag) == 1
    assert prompt.count(skep) == 1
    assert prompt.count(reb) == 1


def test_format_history_compressed_with_empty_tail_shows_summary() -> None:
    out = format_history([], history_summary="el resumen", mode="compressed")
    assert "el resumen" in out
    assert "Sin historial previo" not in out


def test_format_history_compressed_renders_tail() -> None:
    out = format_history([_msg("user", "dato nuevo")], history_summary="resumen", mode="compressed")
    assert "resumen" in out
    assert "dato nuevo" in out


# ── summarize_history: fix A (round ya incrementado) + acumulación ──────


class _FakeSummaryModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, messages: list) -> object:
        self.prompts.append(messages[0].content)
        return type("R", (), {"content": "RESUMEN-GENERADO", "usage_metadata": {}})()


def test_summarize_runs_after_round1_continue(monkeypatch) -> None:
    fake = _FakeSummaryModel()
    monkeypatch.setattr(g, "create_github_model", lambda *a, **k: fake)
    # El moderador ya incrementó round a 2 con status continue
    state = _state(history=list(_ROUND1), round=2, moderator_model="m", summary_model="m")
    result = summarize_history(state)
    assert result.get("history_summary") == "RESUMEN-GENERADO"
    assert result["_summarize_event"]["round"] == 1
    assert "diag R1" in fake.prompts[0]


def test_summarize_is_cumulative(monkeypatch) -> None:
    fake = _FakeSummaryModel()
    monkeypatch.setattr(g, "create_github_model", lambda *a, **k: fake)
    round2 = [
        _msg("diagnostic_agent", "diag R2"),
        _msg("moderator", '{"status": "continue"}'),
    ]
    state = _state(
        history=_ROUND1 + round2, round=3,
        history_summary="RESUMEN-PREVIO", moderator_model="m", summary_model="m",
    )
    result = summarize_history(state)
    assert result["_summarize_event"]["round"] == 2
    prompt = fake.prompts[0]
    assert "RESUMEN-PREVIO" in prompt
    assert "diag R2" in prompt
    assert "diag R1" not in prompt, "solo los mensajes nuevos van en bruto"


def test_summarize_skips_before_any_finished_round(monkeypatch) -> None:
    monkeypatch.setattr(g, "create_github_model", lambda *a, **k: _FakeSummaryModel())
    state = _state(history=[_msg("diagnostic_agent", "diag R1")], round=1, moderator_model="m")
    assert summarize_history(state) == {}
