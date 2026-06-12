from agents_discussion.graph import _decision_from_structured_result
from agents_discussion.prompts import _labels
from agents_discussion.state import ModeratorDecision


_DECISION_JSON = """{
  "status": "continue",
  "confidence": 0.5,
  "leading_hypothesis": "Falta un índice",
  "evidence": [],
  "missing_evidence": [],
  "rejected_hypotheses": [],
  "next_step": "verificar EXPLAIN",
  "recommended_fix": null,
  "risk_level": "low",
  "validation": [],
  "stop_reason": null,
  "flow_directive": null
}"""


class _FakeMsg:
    def __init__(self, content: str, usage: dict | None = None) -> None:
        self.content = content
        self.usage_metadata = usage or {}


def test_parsed_valid_passthrough() -> None:
    decision = ModeratorDecision.model_validate_json(_DECISION_JSON)
    result = {"raw": _FakeMsg("x", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
              "parsed": decision, "parsing_error": None}
    out, usage = _decision_from_structured_result(result)
    assert out is decision
    assert usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def test_fenced_raw_rescued_without_reinvoke() -> None:
    raw = _FakeMsg(f"```json\n{_DECISION_JSON}\n```",
                   {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
    result = {"raw": raw, "parsed": None, "parsing_error": Exception("json_invalid")}
    out, usage = _decision_from_structured_result(result)
    assert out is not None
    assert out.status == "continue"
    assert out.leading_hypothesis == "Falta un índice"
    assert usage["total_tokens"] == 150


def test_raw_with_markdown_wrapper_rescued() -> None:
    raw = _FakeMsg(f"## Decisión del moderador\n\n{_DECISION_JSON}\n\nFin.")
    result = {"raw": raw, "parsed": None, "parsing_error": Exception("x")}
    out, _ = _decision_from_structured_result(result)
    assert out is not None and out.status == "continue"


def test_unrecoverable_raw_returns_none() -> None:
    result = {"raw": _FakeMsg("no hay json aquí"), "parsed": None, "parsing_error": Exception("x")}
    out, usage = _decision_from_structured_result(result)
    assert out is None
    assert usage == {}


def test_bare_decision_passthrough() -> None:
    decision = ModeratorDecision.model_validate_json(_DECISION_JSON)
    out, usage = _decision_from_structured_result(decision)
    assert out is decision
    assert usage == {}


def test_unexpected_type_returns_none() -> None:
    assert _decision_from_structured_result("garbage") == (None, {})


def test_json_only_label_forbids_fences() -> None:
    assert "```" in _labels("es")["json_only"]
    assert "```" in _labels("en")["json_only"]
