"""Import corporate source sections from the user's text bundle as data."""
import re
from pathlib import Path

from app.rag import import_text
from app.settings import ROOT


def import_bundle(repository, source: Path):
    text = source.read_text(encoding="utf-8-sig")
    sections = list(re.finditer(r"^\d+\. ФАЙЛ: (.+)$", text, re.MULTILINE))
    allowed = ("Правила внутреннего трудового распорядка", "Антикоррупционная политика",
               "Положение о коммерческой тайне", "Политика в области качества и экологии")
    result = []
    for i, match in enumerate(sections):
        title = match[1].strip()
        if not title.startswith(allowed):
            continue
        body = text[match.end():sections[i + 1].start() if i + 1 < len(sections) else len(text)].strip("=\r\n ")
        target = ROOT / "data/knowledge" / (f"corporate_{i}.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        version = import_text(repository, target, title=title + " (распознанный текст)", code=f"polyplastic-source-{i}")
        result.append({"title": title, "version_id": version})
    if not result:
        raise ValueError("No recognized corporate policy sections in the text bundle")
    return result
