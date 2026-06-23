# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

"""Token-based cost estimation for LLM calls.

Prices are expressed in USD per 1 million tokens.  The default table covers
the most common models available through GitHub Models and GitHub Copilot.
Users can override or extend the table by pointing MODEL_PRICES_FILE to a
JSON file with the same schema: ``{ "<model>": {"input": <usd/1M>, "output": <usd/1M>} }``.

Because GitHub Models and GitHub Copilot are subscription-based (no per-token
billing), the cost figures are **estimates** based on each model's public API
pricing, useful as a reference for workload sizing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

# ── Default price table (USD per 1M tokens) ──────────────────────────────────
# Sources: OpenAI, Anthropic, Google, Meta, Mistral official pricing pages.
# Last reviewed: 2025-06.

_DEFAULT_PRICES: dict[str, dict[str, float]] = {
    # OpenAI GPT-4o family
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    # OpenAI reasoning / o-series
    "o1": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o3": {"input": 10.00, "output": 40.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    # Anthropic Claude
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-3-7-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    # Google Gemini
    "gemini-2-5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2-5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2-0-flash-001": {"input": 0.10, "output": 0.40},
    "gemini-2-0-flash": {"input": 0.10, "output": 0.40},
    # Meta Llama
    "llama-3-3-70b-instruct": {"input": 0.59, "output": 0.79},
    "llama-3-1-8b-instruct": {"input": 0.18, "output": 0.18},
    # Mistral
    "mistral-large-2411": {"input": 2.00, "output": 6.00},
    "mistral-large": {"input": 2.00, "output": 6.00},
    "mistral-small": {"input": 0.10, "output": 0.30},
    # Microsoft Phi
    "phi-4": {"input": 0.07, "output": 0.14},
    "phi-4-mini": {"input": 0.04, "output": 0.08},
    # Cohere
    "command-r-plus": {"input": 2.50, "output": 10.00},
    "command-r": {"input": 0.15, "output": 0.60},
    "kimi-k2-6": {"input": 0.95, "output": 4.00},
}


def _normalize_name(model: str) -> str:
    """Strip provider prefixes and normalise separators to '-'."""
    name = model
    for prefix in (
        "copilot/Azure/",
        "copilot/",
        "openai/",
        "anthropic/",
        "google/",
        "meta/",
        "mistral-ai/",
        "microsoft/",
        "cohere/",
    ):
        name = name.removeprefix(prefix)
    # normalise '.', '_' → '-' and lowercase
    return name.replace(".", "-").replace("_", "-").lower()


def _load_prices(prices_file: Path | None) -> dict[str, dict[str, float]]:
    """Return the price table, optionally merged with a user-supplied file."""
    prices = dict(_DEFAULT_PRICES)
    if prices_file and Path(prices_file).exists():
        try:
            custom = json.loads(Path(prices_file).read_text(encoding="utf-8"))
            if isinstance(custom, dict):
                prices.update({_normalize_name(k): v for k, v in custom.items()})
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to load MODEL_PRICES_FILE (%s): %s", prices_file, exc)
    return prices


def _find_price(model: str, prices: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """Look up pricing for a model name, using fuzzy prefix/substring matching."""
    normalized = _normalize_name(model)
    # Normalise the price table keys too so dots/underscores don't block matching.
    norm_prices = {_normalize_name(k): v for k, v in prices.items()}
    if normalized in norm_prices:
        return norm_prices[normalized]
    # Try longest key that is fully contained in the normalised name.
    # E.g. "claude-sonnet-4-6" → matches "claude-sonnet-4"
    candidates = [(k, v) for k, v in norm_prices.items() if k in normalized]
    if candidates:
        return max(candidates, key=lambda x: len(x[0]))[1]
    return None


def estimate_cost(
    token_usage: dict[str, dict[str, int]],
    models_by_role: dict[str, str],
    prices_file: Path | None = None,
) -> dict:
    """Compute an estimated cost from accumulated token usage.

    Args:
        token_usage: Mapping of agent_node → {input_tokens, output_tokens, total_tokens}.
        models_by_role: Mapping of agent_node → model name (e.g. ``{"diagnostic_agent": "copilot/gpt-4o"}``).
        prices_file: Optional path to a custom JSON price file.

    Returns:
        Dict with ``by_node`` (per-agent breakdown), ``total_usd`` (sum of all
        costed agents), and ``has_prices`` (False when no price data was found).
    """
    prices = _load_prices(prices_file)
    by_node: dict[str, dict] = {}
    total_usd = 0.0
    has_prices = False

    for node, usage in token_usage.items():
        model = models_by_role.get(node, "")
        price = _find_price(model, prices) if model else None
        in_tok = usage.get("input_tokens", 0) or 0
        out_tok = usage.get("output_tokens", 0) or 0
        if price:
            node_usd = (in_tok * price["input"] + out_tok * price["output"]) / 1_000_000
            has_prices = True
        else:
            node_usd = None
        by_node[node] = {
            "model": model,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": usage.get("total_tokens", in_tok + out_tok),
            "estimated_usd": round(node_usd, 6) if node_usd is not None else None,
        }
        if node_usd is not None:
            total_usd += node_usd

    return {
        "by_node": by_node,
        "total_usd": round(total_usd, 6) if has_prices else None,
        "has_prices": has_prices,
    }
