"""Config-driven model selection + per-model API param safety.

The param tests are the important ones: they prove that switching a role to a
GPT-5 model sends the *right* params (no temperature/seed; max_completion_tokens),
while current gpt-4o models are called exactly as before."""
import app.model_config as mc
from app.services.ai import _call_chat_json, _call_responses_json
from app.services.costing import UsageMeter


# ── config loader ────────────────────────────────────────────────────────────
def test_model_for_known_roles():
    # Returns a real model id for each role (the actual value is config-driven,
    # so we don't hardcode it here - just that every role resolves to a model).
    for role in ("analysis", "research", "interview", "resume_gate"):
        v = mc.model_for(role)
        assert isinstance(v, str) and v


def test_model_for_unknown_role_falls_back():
    assert mc.model_for("does-not-exist") == "gpt-4o"


def test_roles_derived_from_use_for_resolve_to_priced_models():
    # Roles come from each model's "use_for" flag (single source); every role
    # must resolve to a real, priced model.
    for role in ("analysis", "research", "interview", "resume_gate"):
        m = mc.model_for(role)
        assert m and mc.rate_for(m) is not None


def test_priced_but_inactive_model_is_not_assigned():
    # A model with no "use_for" is available/priced but serves no role.
    assert mc.rate_for("gpt-5.4-mini") is not None
    assert all(mc.model_for(r) != "gpt-5.4-mini"
               for r in ("analysis", "research", "interview", "resume_gate"))


def test_rate_for_known_and_unknown():
    assert mc.rate_for("gpt-4o") == (2.50, 10.00)
    assert mc.rate_for("gpt-5.4-mini") == (0.75, 4.50)
    assert mc.rate_for("totally-made-up") is None


def test_is_reasoning_model():
    assert mc.is_reasoning_model("gpt-5.4-mini") is True
    assert mc.is_reasoning_model("gpt-5-nano") is True
    assert mc.is_reasoning_model("o3-mini") is True
    assert mc.is_reasoning_model("gpt-4o") is False
    assert mc.is_reasoning_model("gpt-4o-mini") is False
    assert mc.is_reasoning_model("gpt-4.1-mini") is False


def test_token_budget_from_config():
    assert mc.token_budget("analysis") == 6000
    assert mc.token_budget("interview") == 3500
    assert mc.token_budget("resume_gate") == 300
    assert mc.token_budget("unknown-role") == 4000   # generic safety fallback


def test_reasoning_headroom_applied_only_to_reasoning_models():
    assert mc.reasoning_headroom() == 10000
    assert mc.output_budget("gpt-4o", 6000) == 6000                      # non-reasoning: no headroom
    assert mc.output_budget("gpt-5.4-mini", 6000) == 6000 + mc.reasoning_headroom()


# ── request-body capture harness ─────────────────────────────────────────────
class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.is_success = True
        self.text = ""

    def json(self):
        return self._p


class _CaptureClient:
    def __init__(self, payload):
        self.captured = None
        self._payload = payload

    async def post(self, url, headers=None, json=None, **kw):
        self.captured = json
        return _Resp(self._payload)


_CHAT_OK = {"usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": 1}'}}]}
_RESP_OK = {"usage": {"input_tokens": 10, "output_tokens": 5}, "status": "completed",
            "output": [{"type": "message",
                        "content": [{"type": "output_text", "text": '{"ok": 1}'}]}]}


# ── Chat Completions param safety ────────────────────────────────────────────
async def test_chat_body_current_model_unchanged():
    c = _CaptureClient(_CHAT_OK)
    await _call_chat_json(c, "prompt", 1000, "interview", UsageMeter(),
                          model="gpt-4o-mini", temperature=0, seed=42)
    b = c.captured
    assert b["model"] == "gpt-4o-mini"
    assert b["max_tokens"] == 1000          # classic param
    assert b["temperature"] == 0            # temperature sent
    assert b["seed"] == 42
    assert "max_completion_tokens" not in b
    assert "reasoning_effort" not in b


async def test_chat_body_gpt5_uses_safe_params():
    c = _CaptureClient(_CHAT_OK)
    await _call_chat_json(c, "prompt", 1000, "interview", UsageMeter(),
                          model="gpt-5.4-mini", temperature=0, seed=42)
    b = c.captured
    assert b["model"] == "gpt-5.4-mini"
    assert b["max_completion_tokens"] == 1000 + 10000   # base + reasoning headroom
    assert b["reasoning_effort"] == "low"               # bounded reasoning
    assert "max_tokens" not in b
    assert "temperature" not in b                       # never sent for reasoning models
    assert "seed" not in b


# ── Responses API param safety ───────────────────────────────────────────────
async def test_responses_body_current_model_unchanged():
    c = _CaptureClient(_RESP_OK)
    await _call_responses_json(c, [{"type": "input_text", "text": "x"}], 6000,
                               "analysis", UsageMeter(), model="gpt-4o", temperature=0)
    b = c.captured
    assert b["temperature"] == 0
    assert b["top_p"] == 1
    assert b["max_output_tokens"] == 6000


async def test_responses_body_gpt5_drops_temperature():
    c = _CaptureClient(_RESP_OK)
    await _call_responses_json(c, [{"type": "input_text", "text": "x"}], 6000,
                               "analysis", UsageMeter(), model="gpt-5.4-mini", temperature=0)
    b = c.captured
    assert "temperature" not in b
    assert "top_p" not in b
    assert b["max_output_tokens"] == 6000 + 10000       # base + reasoning headroom
    assert b["reasoning"] == {"effort": "low"}


# ── Research call (web search) param safety ──────────────────────────────────
async def test_research_body_current_model_unchanged(monkeypatch):
    import app.services.ai as ai
    monkeypatch.setattr(ai, "RESEARCH_MODEL", "gpt-4o")
    c = _CaptureClient(_RESP_OK)
    await ai.research_company_interview(c, "Acme", "Engineer", UsageMeter())
    b = c.captured
    assert b["model"] == "gpt-4o"
    assert b["tools"] == [{"type": "web_search_preview"}]
    assert "reasoning" not in b          # gpt-4o: payload identical to before


async def test_research_body_gpt5_adds_reasoning(monkeypatch):
    import app.services.ai as ai
    monkeypatch.setattr(ai, "RESEARCH_MODEL", "gpt-5.4-mini")
    c = _CaptureClient(_RESP_OK)
    await ai.research_company_interview(c, "Acme", "Engineer", UsageMeter())
    b = c.captured
    assert b["reasoning"] == {"effort": "low"}
    assert "temperature" not in b


# ── Resume-gate call param safety (4th and last call site) ───────────────────
async def test_gate_body_current_model_unchanged(monkeypatch):
    import app.services.resume_gate as rg
    monkeypatch.setattr(rg, "RESUME_GATE_MODEL", "gpt-4o-mini")
    c = _CaptureClient(_CHAT_OK)
    await rg.extract_resume_identity(c, "John Doe, Senior Engineer", UsageMeter())
    b = c.captured
    assert b["max_tokens"] == 300
    assert b["temperature"] == 0
    assert "max_completion_tokens" not in b
    assert "reasoning_effort" not in b


async def test_gate_body_gpt5_safe(monkeypatch):
    import app.services.resume_gate as rg
    monkeypatch.setattr(rg, "RESUME_GATE_MODEL", "gpt-5-nano")
    c = _CaptureClient(_CHAT_OK)
    await rg.extract_resume_identity(c, "John Doe, Senior Engineer", UsageMeter())
    b = c.captured
    assert b["max_completion_tokens"] == 300 + 10000
    assert b["reasoning_effort"] == "low"
    assert "temperature" not in b
