"""
llm_util.py

Shared utilities for LLM response handling.
"""

import re


def strip_json_fences(raw: str) -> str:
    """Strip markdown code fences the model occasionally adds despite instructions."""
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
        raw = raw.strip()
    return raw
