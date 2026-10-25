"""Private employee documents -> proposed card fields (never into the corporate corpus).

Plain extraction first (text/OCR + patterns), optional single LLM pass with literal-quote
validation. Every proposal carries document/page/quote; HR accepts or rejects it.
Passport/family data are not extracted: only fields the card and the process need.
Start date, role and organization are never derived from a passport.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from .config import get_settings
from .db import J, tx
from .llm import LLMError, chat_json
from .rag.extract import extract

log = logging.getLogger(__name__)
ALLOWED_EXT = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt"}
MAX_BYTES = 20 * 1024 * 1024
CARD_FIELDS = ["full_name", "phone", "email", "position", "department", "organization", "start_date", "probation_end"]

DATE = r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4})"
MONTHS = {m: i for i, m in enumerate(["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                                      "сентября", "октября", "ноября", "декабря"], 1)}
PATTERNS = {
    "full_name": [r"(?:ФИО|Ф\.И\.О\.|Работник|Сотрудник|Кандидат|Уважаем\w+)\s*[:\-–]?\s*"
                  r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ич|кызы|оглы))"],
    "phone": [r"((?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})"],
    "email": [r"([\w.+-]+@[\w-]+\.[\w.-]+)"],
    "position": [r"(?:на должность|должность|позици\w+)\s*[:\-–]?\s*«?([А-ЯЁа-яё][^\n,;«»]{3,80})"],
    "department": [r"(?:подразделени\w+|отдел\w*|департамент\w*|служб\w+)\s*[:\-–]\s*«?([^\n,;«»]{3,80})"],
    "organization": [r"((?:ООО|АО|ПАО|ЗАО)\s*«[^»]{2,80}»)"],
    "start_date": [r"(?:дата (?:начала работы|выхода|приема|приёма)|приступить к работе|выход на работу|принят\w* на работу)\D{0,20}" + DATE],
    "probation_end": [r"(?:испытательн\w+ срок\w*)\D{0,40}(?:до|по)\s+" + DATE],
}


def _to_iso(value: str) -> str | None:
    value = value.strip()
    m = re.match(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", value)
    try:
        if m:
            y = int(m.group(3))
            y = y + 2000 if y < 100 else y
            return datetime(y, int(m.group(2)), int(m.group(1))).date().isoformat()
        m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", value)
        if m and m.group(2) in MONTHS:
            return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).date().isoformat()
    except ValueError:
        return None
    return None


def regex_fields(pages: list[dict]) -> list[dict]:
    out = []
    for p in pages:
        text = p.get("text") or ""
        for field, pats in PATTERNS.items():
            for pat in pats:
                for m in re.finditer(pat, text, flags=re.I if field not in ("full_name",) else 0):
                    val = m.group(1).strip().rstrip(".")
                    if field in ("start_date", "probation_end"):
                        val = _to_iso(val)
                        if not val:
                            continue
                    if field == "phone":
                        digits = re.sub(r"\D", "", val)
                        val = "+7" + digits[-10:]
                    quote = text[max(0, m.start() - 30):m.end() + 10].replace("\n", " ").strip()
                    out.append({"field": field, "value": val, "page": p["number"], "quote": quote, "method": "regex"})
                    break
    seen, uniq = set(), []
    for f in out:
        k = (f["field"], f["value"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


LLM_SCHEMA = {"type": "object", "properties": {"fields": {"type": "array", "items": {"type": "object", "properties": {
    "field": {"type": "string", "enum": CARD_FIELDS}, "value": {"type": "string"}, "quote": {"type": "string"},
    "page": {"type": "integer"}}, "required": ["field", "value", "quote", "page"]}}}, "required": ["fields"]}


def llm_fields(pages: list[dict]) -> list[dict]:
    text = "\n\n".join(f"[стр. {p['number']}]\n{(p.get('text') or '')[:2500]}" for p in pages[:4])
    system = ("Извлеки из кадрового документа только поля карточки: full_name, phone, email, position, department, "
              "organization, start_date (YYYY-MM-DD), probation_end (YYYY-MM-DD). Для каждого — дословная цитата и "
              "страница. Не придумывай; если поля нет — не включай. Паспортные данные, сведения о семье и детях не "
              "извлекай. Дату выхода, должность и организацию не выводи из паспорта.")
    data = chat_json(system, text, LLM_SCHEMA, max_tokens=500)
    norm = lambda s: re.sub(r"\s+", " ", s.lower()).strip()
    full = {p["number"]: norm(p.get("text") or "") for p in pages}
    out = []
    for f in data.get("fields", []):
        if f.get("field") in CARD_FIELDS and f.get("value") and norm(f.get("quote", "")) in full.get(f.get("page"), ""):
            out.append({"field": f["field"], "value": f["value"].strip(), "page": f["page"],
                        "quote": f["quote"][:300], "method": "llm"})
    return out


def process_document(doc_id: int) -> None:
    s = get_settings()
    with tx() as c:
        d = c.execute("UPDATE employee_documents SET status='processing' WHERE id=%s RETURNING *", (doc_id,)).fetchone()
    try:
        pages = [p.__dict__ for p in extract(s.uploads_dir / d["stored_name"], s.cache_dir)]
        fields = regex_fields(pages)
        try:
            known = {(f["field"], f["value"]) for f in fields}
            fields += [f for f in llm_fields(pages) if (f["field"], f["value"]) not in known]
        except LLMError as e:
            log.info("hr doc llm skipped: %s", e)
        with tx() as c:
            c.execute("UPDATE employee_documents SET status='done', pages=%s, error=%s WHERE id=%s",
                      (J(pages), None if any(p["text"] for p in pages) else "Текст не распознан", doc_id))
            for f in fields:
                c.execute("""INSERT INTO field_suggestions(employee_id, document_id, field, value, page, quote, method)
                             VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                          (d["employee_id"], doc_id, f["field"], f["value"], f["page"], f["quote"], f["method"]))
    except Exception as e:
        log.exception("hr doc %s failed", doc_id)
        with tx() as c:
            c.execute("UPDATE employee_documents SET status='error', error=%s WHERE id=%s", (str(e)[:300], doc_id))
