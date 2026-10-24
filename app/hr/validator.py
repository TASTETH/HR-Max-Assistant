"""Defensive parsing of model JSON, including evidence-quote provenance."""
from __future__ import annotations
import json
import unicodedata
from typing import Any
from pydantic import ValidationError
from .schemas import HRInput, LLMAnalysis

class InvalidLLMResponse(ValueError):
    pass

def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidLLMResponse("Duplicate JSON key")
        result[key] = value
    return result

def _reject_constant(value: str) -> None:
    raise InvalidLLMResponse("Non-finite JSON number")

def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())

def validate_analysis(raw: str, data: HRInput) -> LLMAnalysis:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 64_000:
        raise InvalidLLMResponse("Empty or oversized LLM response")
    content = raw.strip()
    if content.startswith("```json\n") and content.endswith("\n```"):
        content = content[8:-4].strip()
    try:
        value = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        analysis = LLMAnalysis.model_validate(value)
    except (ValueError, ValidationError, RecursionError) as exc:
        raise InvalidLLMResponse("Invalid output JSON/schema") from exc
    seen = set()
    for signal in analysis.signals:
        if signal.category in seen:
            raise InvalidLLMResponse("Repeated category")
        seen.add(signal.category)
        if _normalize(signal.quote) not in _normalize(data.text):
            raise InvalidLLMResponse("Citation not found in employee text")
    return analysis
