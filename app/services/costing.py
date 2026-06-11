"""
Token + cost accounting for every OpenAI call in an analysis.

One UsageMeter is created per analysis. Each OpenAI call records its model,
input tokens and output tokens into it. At the end we read a per-call breakdown
plus a total (tokens + USD), which the analyze router logs and returns.

PRICING: USD per 1,000,000 tokens. Verified against multiple sources April–May
2026. GPT-4o is grandfathered legacy pricing for existing accounts; new accounts
default to the GPT-4.1 family. If you switch models, add a row here — nothing
else needs to change. ALWAYS re-check https://openai.com/api/pricing before
quoting these in anything customer-facing.
"""

from dataclasses import dataclass, field
from typing import List, Optional

# ── Rate table: model -> (input $/1M, output $/1M) ────────────────────────────
# Add new models here as you adopt them; unknown models price at 0 and log a warning.
MODEL_RATES = {
    "gpt-4o":          (2.50, 10.00),
    "gpt-4o-mini":     (0.15,  0.60),
    "gpt-4.1":         (2.00,  8.00),
    "gpt-4.1-mini":    (0.40,  1.60),
    "gpt-4.1-nano":    (0.10,  0.40),
}

# The Responses web_search tool may bill a flat per-call fee on top of tokens.
# This is NOT in the usage object, so we add it explicitly when a call used the
# tool. Set to your actual contracted rate; 0 disables it. (Historically billed
# per 1,000 tool calls — divide accordingly.)
WEB_SEARCH_FEE_PER_CALL = 0.0


@dataclass
class CallCost:
    label: str               # "analysis" | "research" | "interview"
    model: str
    input_tokens: int
    output_tokens: int
    tool_fee: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        rate = MODEL_RATES.get(self.model)
        if rate is None:
            return self.tool_fee  # unknown model: tokens unpriced, keep tool fee
        in_rate, out_rate = rate
        token_cost = (self.input_tokens / 1_000_000) * in_rate \
                   + (self.output_tokens / 1_000_000) * out_rate
        return round(token_cost + self.tool_fee, 6)


@dataclass
class UsageMeter:
    calls: List[CallCost] = field(default_factory=list)

    def record(self, label: str, model: str,
               input_tokens: int, output_tokens: int,
               used_web_search: bool = False) -> None:
        self.calls.append(CallCost(
            label=label,
            model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            tool_fee=WEB_SEARCH_FEE_PER_CALL if used_web_search else 0.0,
        ))

    @property
    def total_input(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def total_output(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 6)

    def as_dict(self) -> dict:
        """Machine-readable summary for the API response."""
        return {
            "total_input_tokens": self.total_input,
            "total_output_tokens": self.total_output,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "calls": [
                {
                    "label": c.label,
                    "model": c.model,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "cost_usd": c.cost_usd,
                }
                for c in self.calls
            ],
        }

    def as_log_line(self) -> str:
        """One-line human-readable breakdown for logs."""
        parts = [
            f"{c.label}[{c.model}] in={c.input_tokens} out={c.output_tokens} ${c.cost_usd:.5f}"
            for c in self.calls
        ]
        return (" | ".join(parts)
                + f" || TOTAL in={self.total_input} out={self.total_output} "
                  f"tok={self.total_tokens} cost=${self.total_cost_usd:.5f}")


def extract_usage(data: dict) -> tuple[int, int]:
    """
    Pull (input_tokens, output_tokens) from either API shape.
    Responses API: usage.input_tokens / usage.output_tokens
    Chat completions: usage.prompt_tokens / usage.completion_tokens
    """
    u = data.get("usage") or {}
    in_tok = u.get("input_tokens", u.get("prompt_tokens", 0)) or 0
    out_tok = u.get("output_tokens", u.get("completion_tokens", 0)) or 0
    return int(in_tok), int(out_tok)
