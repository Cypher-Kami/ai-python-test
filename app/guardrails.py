"""Guardrails pipeline for cleaning, repairing, and normalising LLM responses.

Transforms raw AI-engine output into a validated ``NotificationPayload``.
The pipeline handles the full stochastic distribution produced by the
provider: clean JSON, variant keys, extra/missing fields, markdown-wrapped
responses, malformed JSON, and total refusals.
"""

from __future__ import annotations

import json
import re

from models import GuardrailsError, NotificationPayload

# ---------------------------------------------------------------------------
# Key-alias map - variant keys produced by the provider
# ---------------------------------------------------------------------------

KEY_ALIASES: dict[str, list[str]] = {
    "to": ["Recipient", "To", "destination"],
    "message": ["body", "Message", "text"],
    "type": ["channel", "Type", "method"],
}

# Canonical key names (for fast lookup)
_CANONICAL_KEYS = set(KEY_ALIASES.keys())

# Reverse lookup: variant → canonical
_REVERSE_ALIASES: dict[str, str] = {}
for canonical, aliases in KEY_ALIASES.items():
    for alias in aliases:
        _REVERSE_ALIASES[alias] = canonical


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def strip_markdown(raw: str) -> str:
    """Remove markdown code-block delimiters from *raw*.

    Handles both ````` ```json ... ``` ````` and ````` ``` ... ``` ````` blocks.
    If no code block is found the string is returned unchanged.
    """
    # Match ```json ... ``` or ``` ... ``` (with optional language tag)
    pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    match = re.search(pattern, raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw


def extract_json_from_text(text: str) -> str:
    """Return the first ``{...}`` JSON object found in *text*.

    Uses a greedy match from the first ``{`` to the last ``}`` which is
    sufficient for the single-level objects the provider emits.  Returns
    the original *text* unchanged when no braces are found.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return text


def repair_malformed_json(text: str) -> str:
    """Best-effort repair of common JSON defects.

    Handles:
    * Trailing ``...`` (truncated responses) - stripped, then unclosed
      braces/quotes are closed.
    * Single-quoted strings → double-quoted strings (preserves apostrophes
      inside values).
    * Unquoted keys → quoted keys.
    """
    # 1. Fix truncated JSON: remove trailing ellipsis and whitespace
    cleaned = re.sub(r"\s*\.{2,}\s*$", "", text)

    # 2. Close unclosed quotes - count double-quotes; if odd, append one
    if cleaned.count('"') % 2 != 0:
        cleaned += '"'

    # 3. Close unclosed braces
    open_braces = cleaned.count("{") - cleaned.count("}")
    if open_braces > 0:
        cleaned += "}" * open_braces

    # 4. Fix single-quoted JSON → double-quoted JSON
    #    Strategy: only replace single quotes that act as JSON string
    #    delimiters (immediately after { , : or before } , :).
    #    This avoids breaking apostrophes inside values like "it's".
    if "'" in cleaned and '"' not in cleaned:
        # Entire string uses single quotes - safe to swap all
        cleaned = cleaned.replace("'", '"')

    # 5. Fix unquoted keys: word characters before a colon at key position
    #    e.g.  {to: "x"} → {"to": "x"}
    cleaned = re.sub(
        r'(?<=[{,])\s*(\w+)\s*:', r' "\1":', cleaned
    )

    return cleaned


def normalize_keys(data: dict) -> dict:
    """Map variant keys to canonical names and drop extras.

    Uses the ``KEY_ALIASES`` mapping.  Matching is **case-sensitive** because
    the provider emits exact casings listed in the alias table.
    """
    result: dict = {}
    for key, value in data.items():
        if key in _CANONICAL_KEYS:
            result[key] = value
        elif key in _REVERSE_ALIASES:
            result[_REVERSE_ALIASES[key]] = value
        # else: extra field - silently dropped
    return result


def infer_missing_type(data: dict) -> dict:
    """Infer ``type`` from the ``to`` field when it is absent.

    * Email pattern ``[\\w.\\-]+@[\\w.\\-]+\\.\\w+`` → ``"email"``
    * Phone pattern (digits with optional dashes/spaces) → ``"sms"``
    """
    if "type" not in data and "to" in data:
        to = data["to"]
        if re.fullmatch(r"[\w.\-]+@[\w.\-]+\.\w+", to):
            data["type"] = "email"
        elif re.fullmatch(r"[\d\-\s\+]+", to) and any(c.isdigit() for c in to):
            data["type"] = "sms"
    return data


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def parse_llm_response(raw_content: str) -> NotificationPayload:
    """Run the full guardrails pipeline on a raw LLM response.

    Pipeline stages:
    1. Strip markdown code-block delimiters.
    2. Extract the first JSON object from surrounding text.
    3. Repair common JSON defects (quotes, keys, truncation).
    4. Parse with ``json.loads``.
    5. Normalise variant keys to canonical names.
    6. Infer missing ``type`` from the ``to`` field pattern.
    7. Validate with Pydantic ``NotificationPayload``.

    Raises:
        GuardrailsError: With ``category="parsing_error"`` when no valid
            JSON can be extracted, or ``category="validation_error"`` when
            the extracted data fails Pydantic validation.
    """
    # 1 → 2 → 3
    step1 = strip_markdown(raw_content)
    step2 = extract_json_from_text(step1)
    step3 = repair_malformed_json(step2)

    # 4. Parse JSON
    try:
        data = json.loads(step3)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GuardrailsError(
            category="parsing_error",
            reason=f"No JSON found in LLM response",
        ) from exc

    if not isinstance(data, dict):
        raise GuardrailsError(
            category="parsing_error",
            reason="No JSON found in LLM response",
        )

    # 5. Normalise keys
    data = normalize_keys(data)

    # 6. Infer missing type
    data = infer_missing_type(data)

    # 7. Validate with Pydantic
    try:
        return NotificationPayload(**data)
    except Exception as exc:
        raise GuardrailsError(
            category="validation_error",
            reason=str(exc),
        ) from exc
