import pytest

from app.pipeline.classifier import LLMClassifier, build_prompt, rule_based
from app.schemas import AlertPayload, TriageResult
from tests.conftest import load

EXPECTED = {"div0": "DEVELOPER", "data": "FUNCTIONAL", "rfc": "BASIS"}


@pytest.mark.parametrize("name,category", EXPECTED.items())
def test_rule_based_fallback_categories(name, category):
    r = rule_based(AlertPayload.model_validate(load(name)))
    assert r.category == category
    assert r.fixable_by_agent is (category == "DEVELOPER")


def test_prompt_contains_key_facts():
    text = build_prompt(AlertPayload.model_validate(load("div0")))
    for fact in ("COMPUTE_INT_ZERODIVIDE", "ZTRIAGE_DEMO_BUG", "line 55", "ZBUG_DIV0", "Job canceled"):
        assert fact in text


class _FakeStructured:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    def invoke(self, *_a, **_k):
        if self.exc:
            raise self.exc
        return self.result


class _FakeLLM:
    def __init__(self, structured):
        self.structured = structured

    def with_structured_output(self, schema, method=None):
        return self.structured


def test_llm_failure_falls_back_to_rules():
    clf = LLMClassifier(_FakeLLM(_FakeStructured(exc=TimeoutError("down"))))
    result, source = clf(AlertPayload.model_validate(load("rfc")))
    assert source == "rules" and result.category == "BASIS"


def test_non_developer_never_fixable():
    wrong = TriageResult(category="BASIS", confidence=0.9, summary="s", root_cause="r", recommendation="x",
                         fixable_by_agent=True)
    result, source = LLMClassifier(_FakeLLM(_FakeStructured(result=wrong)))(AlertPayload.model_validate(load("rfc")))
    assert source == "llm" and result.fixable_by_agent is False


@pytest.mark.live
@pytest.mark.parametrize("name,category", EXPECTED.items())
def test_live_llm_classification(name, category):
    """Real OpenRouter call: uv run pytest -m live"""
    from app.config import get_settings
    from app.llm import get_llm

    s = get_settings()
    result, source = LLMClassifier(get_llm(s), method=s.structured_output_method)(AlertPayload.model_validate(load(name)))
    print(f"\n{name}: {result.category} ({result.confidence:.2f}) - {result.root_cause}\n  -> {result.recommendation}")
    assert source == "llm"
    assert result.category == category
