"""Чтение уже подготовленного каталога. Excel в рабочем цикле не используется."""
import json
from pathlib import Path

from .survey import QuestionConfigurationError, validate_question

CATALOG_PATH = Path(__file__).resolve().parent / "data/korsik_scenario.json"


def load_catalog(path=None):
    catalog = json.loads(Path(path or CATALOG_PATH).read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or catalog.get("format_version") != 2 or not catalog.get("events"):
        raise ValueError("Нужен непустой каталог версии 2")
    codes = set()
    for event in catalog["events"]:
        code = event.get("code")
        if not isinstance(code, str) or not code or code in codes:
            raise ValueError("События должны иметь уникальные непустые коды")
        codes.add(code)
        if event.get("phase") not in ("preboarding", "onboarding"):
            raise ValueError("Неизвестная фаза события: " + code)
        for definition in [event, *event.get("variants", {}).values()]:
            question_codes = set()
            for question in definition.get("questions", []):
                question_code, _ = validate_question(question)
                if question_code in question_codes:
                    raise QuestionConfigurationError("Повтор вопроса в событии " + code)
                question_codes.add(question_code)
    return catalog
