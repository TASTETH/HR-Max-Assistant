"""Local extractive RAG: return accessible source text with a locator, or escalate."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

from app.router.schemas import RagAnswer, RagRequest


class PostgresRagBackend:
    def __init__(self, repository):
        self.repository = repository

    async def answer(self, request: RagRequest) -> RagAnswer:
        return await asyncio.to_thread(self._answer, request)

    def _answer(self, request: RagRequest) -> RagAnswer:
        with self.repository.connection() as conn:
            rows = conn.execute("""SELECT c.id,c.content,c.locator,d.title,v.version_no,
                ts_rank_cd(to_tsvector('russian',lower(c.content COLLATE "pg_c_utf8")),websearch_to_tsquery('russian',%s)) AS rank
                FROM korsik.document_chunks c JOIN korsik.document_versions v ON v.id=c.version_id
                JOIN korsik.documents d ON d.id=v.document_id
                WHERE c.version_id=ANY(%s::bigint[]) AND korsik.source_is_accessible(%s,c.id)
                  AND to_tsvector('russian',lower(c.content COLLATE "pg_c_utf8")) @@ websearch_to_tsquery('russian',%s)
                ORDER BY rank DESC,c.id LIMIT 2""",
                (request.question.casefold(), request.allowed_version_ids, request.employee_id, request.question.casefold())).fetchall()
        if not rows:
            return RagAnswer(found=False, text="В доступных материалах не нашёл подтверждённого ответа. Передам вопрос закреплённому HR.",
                             model_version="postgres-fts-extractive-v1")
        # The text is a literal excerpt. No generated policy or invented source links.
        excerpts = [f"{r['content']}\nИсточник: {r['title']}, версия {r['version_no']}, {r['locator']}." for r in rows]
        return RagAnswer(found=True, text="В корпоративных материалах найдено:\n\n" + "\n\n".join(excerpts),
                         chunk_ids=[r["id"] for r in rows], model_version="postgres-fts-extractive-v1")


def import_text(repository, path: Path, *, title: str | None = None, code: str | None = None, org_unit_id=None):
    """Explicit admin operation: importing a document publishes it to its selected scope."""
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("The document is empty")
    digest = hashlib.sha256(text.encode()).digest()
    code = code or "txt-" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
    chunks, current, start = [], [], 1
    for number, line in enumerate(text.splitlines(), 1):
        if sum(len(s) + 1 for s in current) + len(line) > 1800 and current:
            chunks.append((f"строки {start}–{number - 1}", "\n".join(current).strip()))
            current, start = [], number
        # Very long source lines are sliced with an explicit line locator.
        if len(line) > 1800:
            for offset in range(0, len(line), 1800):
                chunks.append((f"строка {number}, символы {offset + 1}–{min(offset + 1800, len(line))}", line[offset:offset + 1800]))
            start = number + 1
        else:
            current.append(line)
    if current and "\n".join(current).strip():
        chunks.append((f"строки {start}–{len(text.splitlines())}", "\n".join(current).strip()))
    with repository.connection() as conn:
        doc = conn.execute("""INSERT INTO korsik.documents(code,title,scope_org_unit_id) VALUES(%s,%s,%s)
            ON CONFLICT(code) DO UPDATE SET title=excluded.title RETURNING id,scope_org_unit_id""",
            (code, title or path.name, org_unit_id)).fetchone()
        if doc["scope_org_unit_id"] != org_unit_id:
            raise ValueError("Changing an existing document's access scope requires a separate review")
        conn.execute("SELECT id FROM korsik.documents WHERE id=%s FOR UPDATE", (doc["id"],))
        existing = conn.execute("SELECT id FROM korsik.document_versions WHERE document_id=%s AND sha256=%s AND is_current", (doc["id"], digest)).fetchone()
        if existing:
            return existing["id"]
        version = conn.execute("SELECT coalesce(max(version_no),0)+1 AS n FROM korsik.document_versions WHERE document_id=%s", (doc["id"],)).fetchone()["n"]
        conn.execute("UPDATE korsik.document_versions SET is_current=false WHERE document_id=%s AND is_current", (doc["id"],))
        vid = conn.execute("""INSERT INTO korsik.document_versions(document_id,version_no,object_uri,sha256,valid_from)
            VALUES(%s,%s,%s,%s,clock_timestamp()) RETURNING id""", (doc["id"], version, path.resolve().as_uri(), digest)).fetchone()["id"]
        for ordinal, (locator, content) in enumerate(chunks, 1):
            if content.strip():
                conn.execute("INSERT INTO korsik.document_chunks(version_id,ordinal,locator,content,qdrant_point_id) VALUES(%s,%s,%s,%s,%s)",
                             (vid, ordinal, locator, content, uuid.uuid4()))
        conn.execute("UPDATE korsik.document_versions SET index_status='ready',indexed_at=clock_timestamp(),is_current=true WHERE id=%s", (vid,))
        return vid
