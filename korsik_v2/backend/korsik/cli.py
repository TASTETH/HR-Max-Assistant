"""Short operational commands. They complement (not replace) the cabinet workflows.

  python -m korsik.cli migrate
  python -m korsik.cli create-hr --login hr --name "Анна HR"          (asks password)
  python -m korsik.cli create-manager --login ivanov --name "Иванов И.И."
  python -m korsik.cli seed-corpus ../seed/corpus --org "ООО «Группа ПОЛИПЛАСТИК»"
  python -m korsik.cli search "можно ли дарить деньги"                (no LLM)
  python -m korsik.cli ask "можно ли дарить деньги" [--employee 1]    (with LLM)
  python -m korsik.cli test-survey --employee 1 --survey check2w --seconds 45
  python -m korsik.cli max-check | subscribe-webhook | subscriptions
  python -m korsik.cli console --employee 1                            (chat as employee via the same pipeline)
  python -m korsik.cli digest-now
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from .config import get_settings
from .db import J, migrate, tx


def _pw(arg):
    if arg:
        return arg
    p1 = getpass.getpass("Пароль (≥10 символов): ")
    if p1 != getpass.getpass("Повторите: "):
        sys.exit("Пароли не совпадают")
    return p1


def cmd_create_user(a, role):
    from .security import hash_password
    migrate()
    with tx() as c:
        c.execute("""INSERT INTO users(login, password_hash, role, display_name, is_hr_head) VALUES (%s,%s,%s,%s,%s)
                     ON CONFLICT (login) DO UPDATE SET password_hash=EXCLUDED.password_hash, display_name=EXCLUDED.display_name,
                     is_hr_head=EXCLUDED.is_hr_head, active=true""",
                  (a.login, hash_password(_pw(a.password)), role, a.name, role == "hr" and not a.no_head))
    print(f"OK: {role} {a.login}" + (" (с правами руководителя HR-направления)" if role == "hr" and not a.no_head else ""))


def cmd_seed_corpus(a):
    """Load corporate documents through the same pipeline as the cabinet (extract -> build -> activate)."""
    import os
    if getattr(a, "bm25_only", False):  # explicit degraded mode when Ollama/bge-m3 is not available yet
        os.environ["EMBED_MODEL"] = ""
        get_settings.cache_clear()
    from .corpus import process_extract, run_build
    from .rag.extract import sha256_file
    s = get_settings()
    migrate()
    s.corpus_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for f in sorted(Path(a.folder).iterdir()):
        if f.suffix.lower() not in (".pdf", ".pptx", ".docx", ".txt"):
            continue
        digest = sha256_file(f)
        with tx() as c:
            ex = c.execute("SELECT id, status FROM corpus_documents WHERE sha256=%s AND status IN ('published','extracted')",
                           (digest,)).fetchone()
        if ex:
            print(f"= {f.name}: уже есть (#{ex['id']}, {ex['status']})")
            if ex["status"] == "extracted":
                ids.append(ex["id"])
            continue
        stored = f"{uuid.uuid4().hex}{f.suffix.lower()}"
        shutil.copy(f, s.corpus_dir / stored)
        # reuse OCR cache shipped with the project if present
        seed_cache = Path(a.folder).parent / "extract_cache"
        if seed_cache.exists():
            s.cache_dir.mkdir(parents=True, exist_ok=True)
            for cf in seed_cache.glob("*.json"):
                if not (s.cache_dir / cf.name).exists():
                    shutil.copy(cf, s.cache_dir / cf.name)
        with tx() as c:
            d = c.execute("""INSERT INTO corpus_documents(title, filename, stored_name, sha256, organizations)
                             VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                          (f.stem, f.name, stored, digest, [a.org])).fetchone()
        print(f"+ {f.name}: извлечение…", flush=True)
        process_extract(d["id"])
        ids.append(d["id"])
    if ids:
        try:
            run_build({"add": ids, "remove": [], "reason": "seed"})
        except Exception as e:
            sys.exit(f"Сборка корпуса не удалась: {e}\nПроверьте Ollama и модель {s.embed_model} "
                     f"(ollama pull {s.embed_model}) или запустите с --bm25-only. Рабочая база не изменена.")
    with tx() as c:
        v = c.execute("SELECT id, dense FROM corpus_versions WHERE status='active'").fetchone()
        docs = c.execute("SELECT title, status, error FROM corpus_documents ORDER BY id").fetchall()
    for d in docs:
        print(f"  {d['status']:10} {d['title']} {d['error'] or ''}")
    print(f"Активная версия корпуса: {v and v['id']} (dense={v and v['dense']})")


def _emp(eid):
    if not eid:
        return None
    with tx() as c:
        return c.execute("SELECT * FROM employees WHERE id=%s", (eid,)).fetchone()


def cmd_search(a):
    from .rag.index import search
    r = search(a.query, _emp(a.employee), use_dense=not a.bm25_only)
    print(f"version={r['version_id']} dense_used={r.get('dense_used')} {r.get('dense_error') or ''}")
    for h in r["hits"][:8]:
        print(f"- «{h['title']}» {h['unit']} {h['page']}: {h['text'][:160].replace(chr(10), ' ')}")


def cmd_ask(a):
    from .rag.answer import answer_question, format_answer
    r = answer_question(a.query, _emp(a.employee), request_id="cli")
    print(f"status={r.status} request_id={r.request_id}")
    print(format_answer(r) if r.status == "answered" else r.text)
    print(json.dumps(r.debug, ensure_ascii=False, default=str)[:1500])


def cmd_test_survey(a):
    from .api.app import schedule_test_survey
    print(schedule_test_survey(a.employee, a.survey, a.seconds, None))


def cmd_max(a, what):
    from .transport import MaxTransport
    s = get_settings()
    t = MaxTransport(s)
    if what == "me":
        print(json.dumps(t.me(), ensure_ascii=False, indent=1))
    elif what == "subs":
        print(json.dumps(t.subscriptions(), ensure_ascii=False, indent=1))
    else:
        if not s.public_base_url:
            sys.exit("Задайте PUBLIC_BASE_URL (https://ваш-домен)")
        print(json.dumps(t.subscribe(s.public_base_url.rstrip("/") + "/webhook/max", s.secret()), ensure_ascii=False))


def cmd_console(a):
    """Type as the employee. Uses webhook storage + worker logic; replies are read from console_outbox.jsonl.
    Requires TRANSPORT=console and a running worker. Not a MAX delivery check."""
    s = get_settings()
    if s.transport != "console":
        sys.exit("Консольный режим: установите TRANSPORT=console и перезапустите worker")
    emp = _emp(a.employee)
    if not emp:
        sys.exit("Нет такого сотрудника")
    uid = emp["max_user_id"] or f"console-user-{emp['id']}"
    chat = emp["max_chat_id"] or f"console-chat-{emp['id']}"
    out = s.data_dir / "console_outbox.jsonl"
    pos = out.stat().st_size if out.exists() else 0
    print("Пишите сообщения; /btn <payload> — нажать кнопку; /invite <token>; Ctrl+C — выход")

    def push(update):
        from .dialog import provider_event_id
        with tx() as c:
            c.execute("INSERT INTO inbound_events(provider_event_id, payload, request_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                      (provider_event_id(update), J(update), uuid.uuid4().hex[:12]))
            c.execute("NOTIFY korsik_jobs")

    while True:
        time.sleep(1.5)
        if out.exists():
            with open(out, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    r = json.loads(line)
                    if r.get("chat_id") == chat or r.get("edit"):
                        b = r["body"]
                        print(f"\nКорсик: {b.get('text')}")
                        for att in b.get("attachments", []):
                            if att["type"] == "inline_keyboard":
                                for row in att["payload"]["buttons"]:
                                    print("   " + " | ".join(f"[{x['text']}] {x.get('payload') or x.get('url')}" for x in row))
                            else:
                                print(f"   (вложение {att['type']})")
                pos = f.tell()
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        ts = int(time.time() * 1000)
        if line.startswith("/btn "):
            push({"update_type": "message_callback", "timestamp": ts,
                  "callback": {"callback_id": uuid.uuid4().hex, "payload": line[5:].strip(), "user": {"user_id": uid}},
                  "message": {"recipient": {"chat_id": chat, "chat_type": "dialog"}}})
        elif line.startswith("/invite "):
            push({"update_type": "bot_started", "timestamp": ts, "chat_id": chat, "user": {"user_id": uid, "name": "console"},
                  "payload": line[8:].strip()})
        elif line:
            push({"update_type": "message_created", "timestamp": ts,
                  "message": {"recipient": {"chat_id": chat, "chat_type": "dialog"}, "sender": {"user_id": uid},
                              "body": {"mid": uuid.uuid4().hex, "text": line}}})


def cmd_rag_eval(a):
    """Retrieval recall@k on the team's question set; with --llm also answer status vs expected.
    Expected sources: document file name + page/slide (from seed/rag_checks.json)."""
    from .rag.index import search
    from .rag.answer import answer_question
    checks = json.loads(Path(a.file).read_text(encoding="utf-8-sig"))
    with tx() as c:
        names = {r["id"]: r["filename"] for r in c.execute("SELECT id, filename FROM corpus_documents").fetchall()}
    emp = {"organization": a.org, "tags": [], "address_form": "ty"}
    hit_n = pos_n = status_ok = 0
    for ch in checks:
        q = ch["question"]
        if ch.get("history"):
            q = " ".join(h["content"] for h in ch["history"] if h["role"] == "user") + " " + q
        r = search(q, emp, use_dense=not a.bm25_only)
        got = [(names.get(h["document_id"]), h["page"]) for h in r["hits"][:a.k]]
        exp = [(s_["document"], s_["location"]) for s_ in ch.get("expected_sources", [])]
        line = f"{ch['id']:4} {ch['expected_status'][:12]:12}"
        if exp:
            pos_n += 1
            ok = any(e in got for e in exp)
            hit_n += ok
            line += f" retrieval@{a.k}={'OK ' if ok else 'MISS'}"
        if a.llm:
            res = answer_question(ch["question"], emp, history=[h["content"] for h in ch.get("history", [])], request_id=ch["id"])
            st_ok = res.status == ch["expected_status"] or (ch["expected_status"] == "insufficient_evidence" and res.status == "needs_clarification")
            status_ok += st_ok
            line += f" status={res.status}{'' if st_ok else ' (ожидалось ' + ch['expected_status'] + ')'}"
        print(line, "|", ch["question"][:70])
    print(f"\nretrieval recall@{a.k}: {hit_n}/{pos_n}" + (f"; статус совпал: {status_ok}/{len(checks)}" if a.llm else ""))


def cmd_digest(a):
    from .analytics import weekly
    with tx() as c:
        for u in c.execute("SELECT * FROM users WHERE is_hr_head AND active").fetchall():
            d = weekly(c, u)
            print(u["login"], "создан" if d else "уже есть за прошлую неделю")


def main():
    p = argparse.ArgumentParser(prog="korsik")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    for name in ("create-hr", "create-manager"):
        x = sub.add_parser(name)
        x.add_argument("--login", required=True)
        x.add_argument("--name", required=True)
        x.add_argument("--password")
        x.add_argument("--no-head", action="store_true", help="HR без прав руководителя HR-направления")
    x = sub.add_parser("seed-corpus")
    x.add_argument("folder")
    x.add_argument("--bm25-only", action="store_true", help="собрать без векторов (если Ollama недоступна)")
    x.add_argument("--org", default="ООО «Группа ПОЛИПЛАСТИК»")
    for name in ("search", "ask"):
        x = sub.add_parser(name)
        x.add_argument("query")
        x.add_argument("--employee", type=int)
        x.add_argument("--bm25-only", action="store_true")
    x = sub.add_parser("test-survey")
    x.add_argument("--employee", type=int, required=True)
    x.add_argument("--survey", default="check2w")
    x.add_argument("--seconds", type=int, default=45)
    sub.add_parser("max-check")
    sub.add_parser("subscriptions")
    sub.add_parser("subscribe-webhook")
    x = sub.add_parser("console")
    x.add_argument("--employee", type=int, required=True)
    sub.add_parser("digest-now")
    x = sub.add_parser("rag-eval")
    x.add_argument("--file", default=str(Path(__file__).resolve().parents[2] / "seed" / "rag_checks.json"))
    x.add_argument("--org", default="ООО «Группа ПОЛИПЛАСТИК»")
    x.add_argument("--k", type=int, default=5)
    x.add_argument("--llm", action="store_true")
    x.add_argument("--bm25-only", action="store_true")
    a = p.parse_args()
    if a.cmd == "migrate":
        print("applied:", migrate())
    elif a.cmd == "create-hr":
        cmd_create_user(a, "hr")
    elif a.cmd == "create-manager":
        a.no_head = True
        cmd_create_user(a, "manager")
    elif a.cmd == "seed-corpus":
        cmd_seed_corpus(a)
    elif a.cmd == "search":
        cmd_search(a)
    elif a.cmd == "ask":
        cmd_ask(a)
    elif a.cmd == "test-survey":
        cmd_test_survey(a)
    elif a.cmd == "max-check":
        cmd_max(a, "me")
    elif a.cmd == "subscriptions":
        cmd_max(a, "subs")
    elif a.cmd == "subscribe-webhook":
        cmd_max(a, "subscribe")
    elif a.cmd == "console":
        cmd_console(a)
    elif a.cmd == "digest-now":
        cmd_digest(a)
    elif a.cmd == "rag-eval":
        cmd_rag_eval(a)


if __name__ == "__main__":
    main()
