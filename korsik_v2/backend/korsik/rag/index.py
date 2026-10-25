"""Corpus versions: chunking, BM25 + dense vectors (Ollama bge-m3), atomic activation.

A version is built completely (texts + BM25 source + vectors in one table, keyed by
corpus_version_id) and only then activated in one transaction. A failed build leaves the
previous active version untouched; versions are never mixed. Retrieval additionally
filters by the document's CURRENT status, so a withdrawn document disappears immediately.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass

import numpy as np
import snowballstemmer

from ..config import get_settings
from ..db import J, tx
from ..llm import LLMError, embed

log = logging.getLogger(__name__)
_stem = snowballstemmer.stemmer("russian")
# negations, contacts and short legal tokens are kept; only pure filler is dropped
STOP = {"и", "в", "во", "на", "с", "со", "к", "ко", "по", "о", "об", "а", "же", "ли", "бы", "то", "это",
        "как", "для", "из", "у", "за", "от", "до", "что", "чтобы", "или", "так", "мне", "меня", "я", "мы",
        "вы", "ты", "он", "она", "они", "его", "ее", "её", "их", "при", "можно", "ли"}
TOKEN = re.compile(r"[a-zа-яё0-9@._+-]+", re.I)

CHUNK_CHARS = 1100
OVERLAP = 180


def tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN.findall(text.lower().replace("ё", "е")):
        t = t.strip("._-+")
        if not t or t in STOP:
            continue
        out.append(t if any(ch.isdigit() for ch in t) or "@" in t else _stem.stemWord(t))
    return out


def chunk_document(doc: dict) -> list[dict]:
    """Slides: one chunk per slide. Pages: paragraph-packed chunks with overlap that also
    carries the tail of the previous page (rules continued on the next page)."""
    pages = doc["pages"] or []
    is_slides = doc["filename"].lower().endswith(".pptx")
    out = []
    prev_tail = ""
    for p in pages:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        if is_slides:
            out.append({"page": p["number"], "text": text})
            continue
        paras = [x.strip() for x in re.split(r"\n\s*\n|\n(?=\d+\.\d+)", text) if x.strip()]
        buf = prev_tail
        for para in paras:
            if len(buf) + len(para) > CHUNK_CHARS and len(buf) > OVERLAP:
                out.append({"page": p["number"], "text": buf.strip()})
                buf = buf[-OVERLAP:]
            buf = (buf + "\n" + para) if buf else para
        if buf.strip():
            out.append({"page": p["number"], "text": buf.strip()})
        prev_tail = text[-OVERLAP:] if not re.search(r"[.;:!?»]\s*$", text) else ""
    return out


def build_version(document_ids: list[int], reason: str) -> int:
    """Build a new corpus version from documents (must be extracted). Returns version id.
    Raises on failure (the version is marked failed, active one unchanged)."""
    s = get_settings()
    with tx() as c:
        v = c.execute("INSERT INTO corpus_versions(document_ids, reason, embed_model) VALUES (%s,%s,%s) RETURNING id",
                      (document_ids, reason, s.embed_model or None)).fetchone()["id"]
        docs = c.execute("SELECT * FROM corpus_documents WHERE id = ANY(%s)", (document_ids,)).fetchall()
    try:
        chunks = []
        for d in docs:
            if not d["pages"]:
                raise ValueError(f"Документ {d['title']} не извлечен")
            for i, ch in enumerate(chunk_document(d)):
                chunks.append({"document_id": d["id"], "ordinal": i, "page": ch["page"], "text": ch["text"],
                               "embed_text": f"{d['title']}. Стр. {ch['page']}. {ch['text']}"})
        if not chunks:
            raise ValueError("В корпусе нет текста")
        vectors = None
        if s.embed_model:
            vectors = embed([c_["embed_text"] for c_ in chunks])  # raises LLMError -> build fails
        with tx() as c:
            for i, ch in enumerate(chunks):
                vec = np.asarray(vectors[i], dtype=np.float32).tobytes() if vectors else None
                c.execute("""INSERT INTO chunks(corpus_version_id, document_id, ordinal, page, text, embedding)
                             VALUES (%s,%s,%s,%s,%s,%s)""",
                          (v, ch["document_id"], ch["ordinal"], ch["page"], ch["text"], vec))
            c.execute("UPDATE corpus_versions SET status='ready', dense=%s WHERE id=%s", (vectors is not None, v))
        return v
    except Exception as e:
        with tx() as c:
            c.execute("UPDATE corpus_versions SET status='failed', error=%s WHERE id=%s", (str(e)[:500], v))
        raise


def activate(conn, version_id: int) -> None:
    conn.execute("UPDATE corpus_versions SET status='archived' WHERE status='active'")
    conn.execute("UPDATE corpus_versions SET status='active', activated_at=now() WHERE id=%s AND status='ready'",
                 (version_id,))


# ---------------------------------------------------------------- in-memory index per version
@dataclass
class Loaded:
    version_id: int
    ids: list[int]
    doc_ids: list[int]
    pages: list[int]
    texts: list[str]
    ordinals: list[int]
    tf: list[Counter]
    df: Counter
    avgdl: float
    vectors: np.ndarray | None


_cache: dict[int, Loaded] = {}
_cache_lock = threading.Lock()


def active_version(conn) -> dict | None:
    return conn.execute("SELECT * FROM corpus_versions WHERE status='active'").fetchone()


def load(version_id: int) -> Loaded:
    with _cache_lock:
        if version_id in _cache:
            return _cache[version_id]
    with tx() as c:
        rows = c.execute("SELECT * FROM chunks WHERE corpus_version_id=%s ORDER BY document_id, ordinal",
                         (version_id,)).fetchall()
    tf = [Counter(tokens(r["text"])) for r in rows]
    df: Counter = Counter()
    for t in tf:
        df.update(t.keys())
    vecs = None
    if rows and all(r["embedding"] is not None for r in rows):
        vecs = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    L = Loaded(version_id, [r["id"] for r in rows], [r["document_id"] for r in rows], [r["page"] for r in rows],
               [r["text"] for r in rows], [r["ordinal"] for r in rows], tf, df,
               (sum(sum(t.values()) for t in tf) / max(1, len(tf))), vecs)
    with _cache_lock:
        _cache.clear()          # keep only the current version in memory
        _cache[version_id] = L
    return L


def bm25(L: Loaded, query: str, allowed: set[int], k: int) -> list[tuple[int, float]]:
    q = tokens(query)
    if not q:
        return []
    N = len(L.tf)
    scores = []
    for i, tf in enumerate(L.tf):
        if L.doc_ids[i] not in allowed:
            continue
        dl = sum(tf.values())
        sc = 0.0
        for t in q:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (N - L.df[t] + 0.5) / (L.df[t] + 0.5))
            sc += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / max(L.avgdl, 1)))
        if sc > 0:                      # no lexical match -> not a lexical candidate
            scores.append((i, sc))
    scores.sort(key=lambda x: -x[1])
    return scores[:k]


def dense(L: Loaded, query: str, allowed: set[int], k: int) -> list[tuple[int, float]]:
    if L.vectors is None:
        return []
    qv = np.asarray(embed([query])[0], dtype=np.float32)
    qv /= np.linalg.norm(qv) + 1e-9
    sims = L.vectors @ qv
    order = [int(i) for i in np.argsort(-sims) if L.doc_ids[int(i)] in allowed]
    return [(i, float(sims[i])) for i in order[:k]]


def rrf(*lists: list[tuple[int, float]], k: int = 60) -> list[int]:
    score: dict[int, float] = {}
    for lst in lists:
        for rank, (i, _) in enumerate(lst):
            score[i] = score.get(i, 0.0) + 1.0 / (k + rank + 1)
    return [i for i, _ in sorted(score.items(), key=lambda x: -x[1])]


_reranker = None


def rerank(query: str, L: Loaded, cand: list[int]) -> list[int]:
    s = get_settings()
    if not s.reranker_model or not cand:
        return cand
    global _reranker
    try:
        if _reranker is None:
            from sentence_transformers import CrossEncoder  # optional dependency
            _reranker = CrossEncoder(s.reranker_model, device="cpu")
        scores = _reranker.predict([(query, L.texts[i]) for i in cand])
        return [i for _, i in sorted(zip(scores, cand), key=lambda x: -x[0])]
    except Exception as e:  # reranker is an optional reorder step
        log.warning("reranker unavailable: %s", type(e).__name__)
        return cand


def allowed_documents(conn, emp: dict | None) -> dict[int, dict]:
    """Published documents applicable to the employee's confirmed organization/tags."""
    rows = conn.execute("SELECT id, title, filename, version_label, organizations, tags FROM corpus_documents WHERE status='published'").fetchall()
    out = {}
    for d in rows:
        orgs = set(d["organizations"])
        if emp is not None:
            if "*" not in orgs and (emp.get("organization") or "") not in orgs:
                continue
            if d["tags"] and not (set(d["tags"]) & set(emp.get("tags") or [])):
                continue
        out[d["id"]] = d
    return out


def search(query: str, emp: dict | None, *, use_dense: bool = True) -> dict:
    s = get_settings()
    with tx() as c:
        v = active_version(c)
        if not v:
            return {"version_id": None, "hits": [], "dense_used": False, "docs": {}}
        docs = allowed_documents(c, emp)
    L = load(v["id"])
    allowed = set(docs)
    lex = bm25(L, query, allowed, s.rag_bm25_k)
    den, dense_used, dense_err = [], False, None
    if use_dense and L.vectors is not None:
        try:
            den = dense(L, query, allowed, s.rag_dense_k)
            dense_used = True
        except LLMError as e:
            dense_err = str(e)
    fused = rerank(query, L, rrf(lex, den)[:12])
    hits = [{"idx": i, "chunk_id": L.ids[i], "document_id": L.doc_ids[i], "page": L.pages[i],
             "text": L.texts[i], "ordinal": L.ordinals[i],
             "title": docs[L.doc_ids[i]]["title"], "version": docs[L.doc_ids[i]]["version_label"],
             "unit": "слайд" if docs[L.doc_ids[i]]["filename"].lower().endswith(".pptx") else "стр."} for i in fused]
    return {"version_id": v["id"], "hits": hits, "dense_used": dense_used, "dense_error": dense_err,
            "docs": docs, "loaded": L}
