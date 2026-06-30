"""
Config-driven models, pricing, and token budgets - SINGLE source of truth.

Everything lives in `models.json`. Each model lists its price and (optionally)
the roles it serves via `"use_for"`. There are NO duplicated config defaults in
this file: to change a model, a price, or a budget, you edit `models.json` and
nothing else.

If `models.json` cannot be read at all, we fall back to ONE emergency model
(`gpt-4o` for every role) and log loudly. That is a keep-the-app-alive safety
net, not configuration - fix `models.json`.

GPT-5 / o-series are reasoning models and need different API params than gpt-4o
(no temperature/seed; max_completion_tokens; reasoning_effort; output headroom).
`is_reasoning_model()` drives that, so a switch in `models.json` doesn't break
the OpenAI calls.
"""
import json
import logging
import os

logger = logging.getLogger("applyin.models")

_ROLE_NAMES = ("resume_gate", "analysis", "research", "interview")
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "models.json")

# Used ONLY if models.json is unreadable. This is a safety net, not config.
_EMERGENCY_MODEL = "gpt-4o"
_EMERGENCY_RATE = {"input": 2.50, "output": 10.00}
# Generic fallbacks for the (numeric) global knobs if a key is absent.
_FALLBACK_BUDGET = 4000
_FALLBACK_HEADROOM = 10000


def _emergency():
    logger.error("models.json unusable - falling back to '%s' for every role. FIX models.json.",
                 _EMERGENCY_MODEL)
    roles = {r: _EMERGENCY_MODEL for r in _ROLE_NAMES}
    return roles, {_EMERGENCY_MODEL: dict(_EMERGENCY_RATE)}, "", {}, _FALLBACK_HEADROOM


def _load():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Could not read models.json (%s)", e)
        return _emergency()

    models = data.get("models")
    if not isinstance(models, dict) or not models:
        logger.error("models.json has no 'models' map")
        return _emergency()

    # Pricing comes straight from each model entry.
    pricing = {}
    for m, p in models.items():
        if isinstance(p, dict) and "input" in p and "output" in p:
            pricing[m] = {"input": float(p["input"]), "output": float(p["output"])}

    # Roles are DERIVED from each model's "use_for" flag (no separate roles map).
    roles = {}
    for m, p in models.items():
        if not isinstance(p, dict):
            continue
        for role in (p.get("use_for") or []):
            if role in roles:
                logger.warning("Role %r assigned to multiple models; keeping %r, ignoring %r",
                               role, roles[role], m)
            else:
                roles[role] = m

    # Every role must resolve to a (priced) model.
    for role in _ROLE_NAMES:
        if role not in roles:
            logger.error("Role %r has no model (nothing lists it in 'use_for'); using %s",
                         role, _EMERGENCY_MODEL)
            roles[role] = _EMERGENCY_MODEL
            pricing.setdefault(_EMERGENCY_MODEL, dict(_EMERGENCY_RATE))

    budgets = {}
    for role, v in (data.get("token_budgets") or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            budgets[role] = int(v)

    hr = data.get("reasoning_headroom", _FALLBACK_HEADROOM)
    headroom = int(hr) if isinstance(hr, (int, float)) and hr >= 0 else _FALLBACK_HEADROOM

    eff = data.get("reasoning_effort", "")
    effort = eff if isinstance(eff, str) else ""

    logger.info("models.json loaded | roles=%s budgets=%s headroom=%s", roles, budgets, headroom)
    return roles, pricing, effort, budgets, headroom


_ROLES, _PRICING, _EFFORT, _BUDGETS, _HEADROOM = _load()


def model_for(role: str) -> str:
    """The model id serving a role (per `use_for` in models.json)."""
    return _ROLES.get(role) or _EMERGENCY_MODEL


def rate_for(model: str):
    """(input_$per_1M, output_$per_1M) for a model, or None if unpriced."""
    p = _PRICING.get(model)
    return (p["input"], p["output"]) if p else None


def all_rates() -> dict:
    """All known model -> (in, out) rates (for the cost table / diagnostics)."""
    return {m: (p["input"], p["output"]) for m, p in _PRICING.items()}


def is_reasoning_model(model: str) -> bool:
    """True for GPT-5 and o-series reasoning models, which take different API
    params than gpt-4o (no temperature/seed; max_completion_tokens; reasoning_effort)."""
    m = (model or "").lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def reasoning_effort() -> str:
    """Configured reasoning effort (empty string => use effort_or_default's 'low')."""
    return _EFFORT


def effort_or_default() -> str:
    """Reasoning effort to send for reasoning models (config value, or 'low')."""
    return _EFFORT or "low"


def token_budget(role: str) -> int:
    """Configured base output-token budget for a role (from models.json)."""
    return _BUDGETS.get(role, _FALLBACK_BUDGET)


def reasoning_headroom() -> int:
    """Extra output budget (from config) added for reasoning models."""
    return _HEADROOM


def output_budget(model: str, base: int) -> int:
    """Output-token budget: `base`, plus the configured reasoning headroom for
    reasoning models (their thinking tokens count toward the output budget)."""
    return base + _HEADROOM if is_reasoning_model(model) else base
