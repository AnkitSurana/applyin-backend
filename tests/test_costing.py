"""UsageMeter: token/cost accounting + the new per-call duration_ms timing."""
from app.services.costing import UsageMeter, extract_usage


def test_records_duration_ms():
    m = UsageMeter()
    m.record("analysis", "gpt-4o", 1000, 2000, duration_ms=4500)
    call = m.as_dict()["calls"][0]
    assert call["duration_ms"] == 4500
    assert call["label"] == "analysis"


def test_duration_defaults_to_zero():
    m = UsageMeter()
    m.record("analysis", "gpt-4o", 1, 1)
    assert m.as_dict()["calls"][0]["duration_ms"] == 0


def test_totals():
    m = UsageMeter()
    m.record("a", "gpt-4o", 100, 200)
    m.record("b", "gpt-4o-mini", 10, 20)
    assert m.total_input == 110
    assert m.total_output == 220
    assert m.total_tokens == 330


def test_cost_gpt4o_known_rate():
    m = UsageMeter()
    m.record("analysis", "gpt-4o", 1_000_000, 1_000_000)
    # 1M input * $2.50 + 1M output * $10.00 = $12.50
    assert abs(m.total_cost_usd - 12.50) < 1e-6


def test_cost_unknown_model_is_zero():
    m = UsageMeter()
    m.record("x", "totally-made-up-model", 1000, 1000)
    assert m.total_cost_usd == 0.0


def test_extract_usage_responses_api_shape():
    assert extract_usage({"usage": {"input_tokens": 5, "output_tokens": 7}}) == (5, 7)


def test_extract_usage_chat_completions_shape():
    assert extract_usage({"usage": {"prompt_tokens": 3, "completion_tokens": 9}}) == (3, 9)


def test_extract_usage_missing_is_zero():
    assert extract_usage({}) == (0, 0)
    assert extract_usage({"usage": {}}) == (0, 0)
