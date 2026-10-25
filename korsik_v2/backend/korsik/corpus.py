"""HR-managed knowledge base: upload -> extract (background) -> review -> publish (new corpus
version built and activated atomically) -> withdraw/replace. Failures keep the working corpus."""
from __future__ import annotations

import logging

from .config import get_settings
from .db import J, enqueue, tx
from .rag.extract import extract
from .rag.index import activate, build_version

log = logging.getLogger(__name__)
ALLOWED_EXT = {".pdf", ".pptx", ".docx", ".txt", ".png", ".jpg", ".jpeg"}
MAX_BYTES = 80 * 1024 * 1024


def process_extract(doc_id: int) -> None:
    s = get_settings()
    with tx() as c:
        d = c.execute("UPDATE corpus_documents SET status='processing', error=NULL WHERE id=%s RETURNING *",
                      (doc_id,)).fetchone()
    try:
        pages = [p.__dict__ for p in extract(s.corpus_dir / d["stored_name"], s.cache_dir)]
        empty = [p["number"] for p in pages if not (p["text"] or "").strip()]
        if not any((p["text"] or "").strip() for p in pages):
            raise ValueError("Не удалось извлечь текст ни с одной страницы")
        with tx() as c:
            c.execute("UPDATE corpus_documents SET status='extracted', pages=%s, error=%s WHERE id=%s",
                      (J(pages), f"Пустые страницы: {empty}" if empty else None, doc_id))
    except Exception as e:
        log.exception("corpus extract %s failed", doc_id)
        with tx() as c:
            c.execute("UPDATE corpus_documents SET status='error', error=%s WHERE id=%s", (str(e)[:400], doc_id))


def request_publish(conn, doc_id: int, user_id: int) -> None:
    d = conn.execute("SELECT status FROM corpus_documents WHERE id=%s", (doc_id,)).fetchone()
    if d["status"] not in ("extracted", "error") or not conn.execute(
            "SELECT pages FROM corpus_documents WHERE id=%s", (doc_id,)).fetchone()["pages"]:
        raise ValueError("Документ ещё не подготовлен")
    enqueue(conn, "corpus_build", {"add": [doc_id], "remove": [], "by": user_id, "reason": f"publish:{doc_id}"},
            dedupe_key=f"corpus_build:publish:{doc_id}:{conn.execute('SELECT now()').fetchone()['now'].timestamp()}")


def request_withdraw(conn, doc_id: int, user_id: int) -> None:
    # immediate effect: retrieval and the sender only accept 'published' documents
    conn.execute("UPDATE corpus_documents SET status='withdrawn' WHERE id=%s AND status='published'", (doc_id,))
    enqueue(conn, "corpus_build", {"add": [], "remove": [doc_id], "by": user_id, "reason": f"withdraw:{doc_id}"},
            dedupe_key=f"corpus_build:withdraw:{doc_id}:{conn.execute('SELECT now()').fetchone()['now'].timestamp()}")


def run_build(payload: dict) -> None:
    add, remove = payload.get("add", []), payload.get("remove", [])
    with tx() as c:
        current = [r["id"] for r in c.execute("SELECT id FROM corpus_documents WHERE status='published'").fetchall()]
        replaced = [r["replaces_id"] for r in c.execute(
            "SELECT replaces_id FROM corpus_documents WHERE id = ANY(%s) AND replaces_id IS NOT NULL", (add,)).fetchall()]
    ids = sorted((set(current) | set(add)) - set(remove) - set(replaced))
    if not ids:
        with tx() as c:
            c.execute("UPDATE corpus_versions SET status='archived' WHERE status='active'")
        return
    try:
        v = build_version(ids, payload.get("reason", ""))
    except Exception as e:
        with tx() as c:
            c.execute("UPDATE corpus_documents SET status='error', error=%s WHERE id = ANY(%s) AND status <> 'published'",
                      (f"Сборка корпуса не удалась: {str(e)[:300]}. Рабочая база не изменена.", add))
        raise
    with tx() as c:
        activate(c, v)
        c.execute("UPDATE corpus_documents SET status='published', published_at=now(), error=NULL WHERE id = ANY(%s)", (add,))
        if replaced:
            c.execute("UPDATE corpus_documents SET status='superseded' WHERE id = ANY(%s)", (replaced,))
