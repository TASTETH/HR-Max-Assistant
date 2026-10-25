"""HTTP API: MAX webhook, HR cabinet, manager views. Object-level checks on every endpoint."""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import analytics, cases as cases_mod, corpus as corpus_mod
from ..config import get_settings
from ..db import J, audit, enqueue, migrate, new_request_id, tx
from ..dialog import provider_event_id, token_hash
from ..hrdocs import ALLOWED_EXT as HR_EXT, MAX_BYTES as HR_MAX
from ..llm import health as llm_health
from ..logging_setup import setup_logging
from ..scenario import engine
from ..security import COOKIE, new_invite_token, new_session, session_user, verify_password

log = logging.getLogger("korsik.api")
app = FastAPI(title="Корсик", docs_url=None, redoc_url=None, openapi_url=None)
FRONT = Path(__file__).resolve().parents[2] / "frontend_dist"


@app.on_event("startup")
def _startup():
    setup_logging()
    migrate()
    with tx() as c:
        engine.register_scenario(c)
    s = get_settings()
    for d in (s.uploads_dir, s.corpus_dir, s.cache_dir, s.data_dir / "materials"):
        d.mkdir(parents=True, exist_ok=True)


@app.middleware("http")
async def request_log(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or new_request_id()
    request.state.rid = rid
    t0 = time.monotonic()
    response = await call_next(request)
    if not request.url.path.startswith("/assets"):
        log.info("http request_id=%s %s %s -> %s %.0fms", rid, request.method, request.url.path,
                 response.status_code, (time.monotonic() - t0) * 1000)
    response.headers["X-Request-Id"] = rid
    return response


# ---------------------------------------------------------------- webhook
@app.post("/webhook/max")
async def webhook(request: Request):
    s = get_settings()
    secret = s.secret()
    if not hmac.compare_digest(request.headers.get("X-Max-Bot-Api-Secret", "").encode(), secret.encode()):
        raise HTTPException(401, "invalid secret")
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "too large")
    try:
        update = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "bad json")
    if not isinstance(update, dict):
        raise HTTPException(400, "bad update")
    with tx() as c:  # store once; processing happens in the worker
        c.execute("""INSERT INTO inbound_events(provider_event_id, payload, request_id) VALUES (%s,%s,%s)
                     ON CONFLICT (provider_event_id) DO NOTHING""",
                  (provider_event_id(update), J(update), request.state.rid))
        c.execute("NOTIFY korsik_jobs")
    return {"ok": True}


# ---------------------------------------------------------------- health
@app.get("/health")
def health():
    return {"status": "alive"}


@app.get("/ready")
def ready():
    out: dict[str, Any] = {}
    try:
        with tx() as c:
            c.execute("SELECT 1")
            v = c.execute("SELECT id, dense FROM corpus_versions WHERE status='active'").fetchone()
            out["db"] = "ok"
            out["corpus"] = {"active_version": v["id"], "dense": v["dense"]} if v else "no_active_corpus"
            out["queue"] = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, count(*) n FROM outbox GROUP BY status").fetchall()}
    except Exception as e:
        out["db"] = f"error:{type(e).__name__}"
    out["llm"] = llm_health()
    s = get_settings()
    out["transport"] = s.transport
    out["max_token_configured"] = bool(s.max_bot_token.get_secret_value())
    ok = out.get("db") == "ok"
    return JSONResponse(out, status_code=200 if ok else 503)


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    login: str
    password: str


_failed: dict[str, list[float]] = {}


@app.post("/api/login")
def login(body: LoginIn, response: Response, request: Request):
    key = request.client.host if request.client else "?"
    attempts = [t for t in _failed.get(key, []) if t > time.time() - 300]
    if len(attempts) >= 10:
        raise HTTPException(429, "Слишком много попыток, подождите 5 минут")
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE login=%s AND active", (body.login.strip(),)).fetchone()
        if not u or not verify_password(body.password, u["password_hash"]):
            _failed[key] = attempts + [time.time()]
            raise HTTPException(401, "Неверный логин или пароль")
        token, csrf = new_session(c, u["id"])
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=get_settings().cookie_secure,
                        max_age=get_settings().session_hours * 3600)
    return {"user": _user_out(u), "csrf": csrf}


def _user_out(u):
    return {"id": u["id"], "login": u["login"], "role": u["role"], "is_hr_head": u["is_hr_head"],
            "display_name": u["display_name"], "max_linked": bool(u.get("max_linked_at"))}


def current_user(request: Request) -> dict:
    with tx() as c:
        u = session_user(c, request.cookies.get(COOKIE))
    if not u:
        raise HTTPException(401, "Требуется вход")
    if request.method not in ("GET", "HEAD") and request.headers.get("X-CSRF") != u["csrf"]:
        raise HTTPException(403, "CSRF")
    return u


def hr_user(u: dict = Depends(current_user)) -> dict:
    if u["role"] != "hr":
        raise HTTPException(403, "Только для HR")
    return u


@app.get("/api/me")
def me(u: dict = Depends(current_user)):
    return {"user": _user_out(u), "csrf": u["csrf"], "bot_url": get_settings().max_bot_url}


@app.post("/api/logout")
def logout(response: Response, u: dict = Depends(current_user), request: Request = None):
    from ..security import sha
    with tx() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=%s", (sha(request.cookies.get(COOKIE, "")),))
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------- employees
EMP_FIELDS = ["full_name", "short_name", "phone", "email", "organization", "department", "position", "work_format",
              "tags", "start_date", "timezone", "probation_end", "address_form", "manager_user_id", "it_access_confirmed"]
TAGS = {"GrPP", "GTP", "Luzhniki"}


class EmployeeIn(BaseModel):
    full_name: str | None = None
    short_name: str | None = None
    phone: str | None = None
    email: str | None = None
    organization: str | None = None
    department: str | None = None
    position: str | None = None
    work_format: str | None = Field(None, pattern="^(office|production)$")
    tags: list[str] | None = None
    start_date: date | None = None
    timezone: str | None = None
    probation_end: date | None = None
    address_form: str | None = Field(None, pattern="^(ty|vy)$")
    manager_user_id: int | None = None
    it_access_confirmed: bool | None = None


def _validate_emp(c, data: dict):
    if "tags" in data and data["tags"] is not None:
        bad = set(data["tags"]) - TAGS
        if bad:
            raise HTTPException(400, f"Неизвестные теги: {', '.join(bad)}")
    if data.get("timezone"):
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(data["timezone"])
        except Exception:
            raise HTTPException(400, "Неизвестный часовой пояс")
    if data.get("manager_user_id"):
        m = c.execute("SELECT role FROM users WHERE id=%s AND active", (data["manager_user_id"],)).fetchone()
        if not m or m["role"] != "manager":
            raise HTTPException(400, "Руководитель не найден")


def _emp_or_404(c, emp_id: int) -> dict:
    e = c.execute("SELECT * FROM employees WHERE id=%s", (emp_id,)).fetchone()
    if not e:
        raise HTTPException(404, "Сотрудник не найден")
    return e


@app.get("/api/employees")
def list_employees(q: str = "", status: str = "", tag: str = "", u: dict = Depends(hr_user)):
    with tx() as c:
        rows = c.execute(
            """SELECT e.*, m.display_name AS manager_name,
                      (SELECT count(*) FROM cases x WHERE x.employee_id=e.id AND x.status<>'resolved') open_cases
               FROM employees e LEFT JOIN users m ON m.id=e.manager_user_id
               WHERE (%(q)s = '' OR e.full_name ILIKE '%%'||%(q)s||'%%' OR e.department ILIKE '%%'||%(q)s||'%%'
                      OR e.position ILIKE '%%'||%(q)s||'%%')
                 AND (%(st)s = '' OR e.status=%(st)s) AND (%(tag)s = '' OR %(tag)s = ANY(e.tags))
               ORDER BY e.created_at DESC""", {"q": q, "st": status, "tag": tag}).fetchall()
        today = datetime.now(timezone.utc).date()
        out = []
        for r in rows:
            out.append({**_emp_public(r), "manager_name": r["manager_name"], "open_cases": r["open_cases"],
                        "stage": analytics.stage_of(r, today), "progress": analytics.progress(c, r["id"])})
    return out


def _emp_public(e: dict) -> dict:
    d = {k: e.get(k) for k in ["id", "status", "dialog_mode", "preonboarding_state", "linked_at", "created_at",
                               "intro_text", "intro_approved_at", *EMP_FIELDS]}
    d["max_linked"] = bool(e.get("max_user_id"))
    d["intro_photo"] = bool(e.get("intro_photo_ref"))
    d["effective_probation_end"] = engine.effective_probation_end(e) if e.get("start_date") else None
    return d


@app.post("/api/employees")
def create_employee(body: EmployeeIn, u: dict = Depends(hr_user)):
    data = body.model_dump(exclude_none=True)
    with tx() as c:
        _validate_emp(c, data)
        hr = cases_mod.single_hr(c)  # responsible HR is assigned by the server
        data.setdefault("timezone", get_settings().default_timezone)
        cols = list(data)
        row = c.execute(f"INSERT INTO employees({', '.join(cols + ['hr_user_id'])}) VALUES ({', '.join(['%s'] * (len(cols) + 1))}) RETURNING *",
                        [*data.values(), hr["id"]]).fetchone()
        audit(c, u["id"], "create_employee", f"employee:{row['id']}")
    return _emp_public(row)


@app.patch("/api/employees/{emp_id}")
def update_employee(emp_id: int, body: EmployeeIn, u: dict = Depends(hr_user)):
    data = body.model_dump(exclude_unset=True)
    with tx() as c:
        e = _emp_or_404(c, emp_id)
        _validate_emp(c, data)
        if not data:
            return _emp_public(e)
        sets = ", ".join(f"{k}=%s" for k in data)
        row = c.execute(f"UPDATE employees SET {sets}, updated_at=now() WHERE id=%s RETURNING *",
                        [*data.values(), emp_id]).fetchone()
        moved = 0
        if row["status"] == "active" and any(k in data for k in ("start_date", "probation_end", "timezone", "tags", "work_format")):
            moved = engine.reschedule_future(c, row)
        audit(c, u["id"], "update_employee", f"employee:{emp_id}", {"fields": list(data)})
    return {**_emp_public(row), "rescheduled": moved}


@app.get("/api/employees/{emp_id}")
def get_employee(emp_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        e = _emp_or_404(c, emp_id)
        docs = c.execute("""SELECT id, filename, mime, size_bytes, status, error, created_at FROM employee_documents
                            WHERE employee_id=%s ORDER BY id""", (emp_id,)).fetchall()
        sugg = c.execute("SELECT * FROM field_suggestions WHERE employee_id=%s ORDER BY field, id", (emp_id,)).fetchall()
        invites = c.execute("""SELECT id, kind, expires_at, revoked_at, used_at, created_at FROM invites
                               WHERE employee_id=%s ORDER BY id DESC""", (emp_id,)).fetchall()
        events = c.execute("SELECT * FROM events WHERE employee_id=%s ORDER BY due_at", (emp_id,)).fetchall()
        runs = c.execute("SELECT * FROM survey_runs WHERE employee_id=%s ORDER BY id", (emp_id,)).fetchall()
        answers = c.execute("""SELECT a.*, r.survey_key FROM survey_answers a JOIN survey_runs r ON r.id=a.run_id
                               WHERE a.employee_id=%s ORDER BY a.run_id, a.id""", (emp_id,)).fetchall()
        msgs = c.execute("""SELECT m.id, m.direction, m.author_kind, m.text, m.created_at, m.case_id, m.payload,
                                   o.status AS delivery, o.last_error AS delivery_error, u.display_name AS author
                            FROM messages m LEFT JOIN outbox o ON o.message_id=m.id
                            LEFT JOIN users u ON u.id=m.author_user_id
                            WHERE m.employee_id=%s ORDER BY m.id DESC LIMIT 300""", (emp_id,)).fetchall()
        cs = c.execute("SELECT * FROM cases WHERE employee_id=%s ORDER BY id DESC", (emp_id,)).fetchall()
        mats = c.execute("SELECT id, key, title, kind, filename, url, created_at FROM materials WHERE employee_id=%s AND active",
                         (emp_id,)).fetchall()
        an = c.execute("SELECT * FROM analyses WHERE employee_id=%s ORDER BY id DESC LIMIT 50", (emp_id,)).fetchall()
        prog = analytics.progress(c, emp_id)
    titles = {k: v["title"] for k, v in engine.scenario()["surveys"].items()}
    return {"employee": _emp_public(e), "documents": docs, "suggestions": sugg, "invites": invites,
            "events": [{**ev, "state": None, "warnings": (ev["state"] or {}).get("warnings", []),
                        "waiting": (ev["state"] or {}).get("waiting")} for ev in events],
            "surveys": [{**r, "title": titles.get(r["survey_key"].split("#")[0], r["survey_key"])} for r in runs],
            "answers": answers, "messages": list(reversed(msgs)), "cases": cs, "materials": mats,
            "analyses": an, "progress": prog, "activation_missing": engine.activation_problems(e)}


@app.post("/api/employees/{emp_id}/activate")
def activate(emp_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        e = c.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (emp_id,)).fetchone()
        if not e:
            raise HTTPException(404)
        miss = engine.activation_problems(e)
        if miss:
            raise HTTPException(400, "Не заполнено: " + ", ".join(miss))
        c.execute("""UPDATE employees SET status='active', scenario_code=%s, scenario_started_at=now(),
                     updated_at=now() WHERE id=%s""", (engine.scenario()["code"], emp_id))
        e = c.execute("SELECT * FROM employees WHERE id=%s", (emp_id,)).fetchone()
        res = engine.materialize_events(c, e)
        audit(c, u["id"], "activate", f"employee:{emp_id}", res)
    return res


@app.post("/api/employees/{emp_id}/status/{new}")
def set_status(emp_id: int, new: str, u: dict = Depends(hr_user)):
    if new not in ("paused", "active", "deactivated", "completed"):
        raise HTTPException(400)
    with tx() as c:
        e = _emp_or_404(c, emp_id)
        if new == "active" and e["status"] == "draft":
            raise HTTPException(400, "Используйте «Запустить сопровождение»")
        c.execute("UPDATE employees SET status=%s, updated_at=now() WHERE id=%s", (new, emp_id))
        if new == "deactivated":
            c.execute("UPDATE events SET status='cancelled', updated_at=now() WHERE employee_id=%s AND status IN ('scheduled','waiting_reply')", (emp_id,))
            c.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp_id,))
            c.execute("UPDATE survey_runs SET status='cancelled', next_reminder_at=NULL WHERE employee_id=%s AND status IN ('active','queued')", (emp_id,))
        audit(c, u["id"], "set_status", f"employee:{emp_id}", {"status": new})
    return {"ok": True}


def _safe_store(upload: UploadFile, folder: Path, allowed: set[str], max_bytes: int) -> tuple[str, int, str]:
    import hashlib
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Формат не поддерживается: {ext or 'без расширения'}")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    h, size = hashlib.sha256(), 0
    with open(folder / name, "wb") as f:
        while chunk := upload.file.read(1 << 20):
            size += len(chunk)
            if size > max_bytes:
                f.close()
                (folder / name).unlink(missing_ok=True)
                raise HTTPException(413, "Файл слишком большой")
            h.update(chunk)
            f.write(chunk)
    return name, size, h.hexdigest()


@app.post("/api/employees/{emp_id}/documents")
def upload_document(emp_id: int, file: UploadFile = File(...), u: dict = Depends(hr_user)):
    s = get_settings()
    name, size, digest = _safe_store(file, s.uploads_dir, HR_EXT, HR_MAX)
    with tx() as c:
        _emp_or_404(c, emp_id)
        d = c.execute("""INSERT INTO employee_documents(employee_id, filename, stored_name, mime, size_bytes, sha256, uploaded_by)
                         VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                      (emp_id, Path(file.filename).name[:200], name, file.content_type or "", size, digest, u["id"])).fetchone()
        enqueue(c, "hr_doc", {"document_id": d["id"]}, dedupe_key=f"hr_doc:{d['id']}")
        audit(c, u["id"], "upload_document", f"employee:{emp_id}", {"document": d["id"]})
    return {"id": d["id"]}


@app.get("/api/documents/{doc_id}/file")
def download_document(doc_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        d = c.execute("SELECT * FROM employee_documents WHERE id=%s", (doc_id,)).fetchone()
        if not d:
            raise HTTPException(404)
        audit(c, u["id"], "download_document", f"document:{doc_id}")
    return FileResponse(get_settings().uploads_dir / d["stored_name"], filename=d["filename"])


@app.get("/api/documents/{doc_id}/text")
def document_text(doc_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        d = c.execute("SELECT id, filename, pages, status, error FROM employee_documents WHERE id=%s", (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404)
    return d


@app.post("/api/suggestions/{sid}/{action}")
def decide_suggestion(sid: int, action: str, u: dict = Depends(hr_user)):
    if action not in ("accept", "reject"):
        raise HTTPException(400)
    with tx() as c:
        sg = c.execute("SELECT * FROM field_suggestions WHERE id=%s FOR UPDATE", (sid,)).fetchone()
        if not sg:
            raise HTTPException(404)
        c.execute("UPDATE field_suggestions SET status=%s, decided_by=%s WHERE id=%s",
                  ("accepted" if action == "accept" else "rejected", u["id"], sid))
        if action == "accept":
            val: Any = sg["value"]
            if sg["field"] in ("start_date", "probation_end"):
                val = date.fromisoformat(val)
            c.execute(f"UPDATE employees SET {sg['field']}=%s, updated_at=now() WHERE id=%s", (val, sg["employee_id"]))
            if sg["field"] == "full_name":
                parts = sg["value"].split()
                if len(parts) >= 2:
                    c.execute("UPDATE employees SET short_name=%s WHERE id=%s AND short_name=''", (parts[1], sg["employee_id"]))
            e = c.execute("SELECT * FROM employees WHERE id=%s", (sg["employee_id"],)).fetchone()
            if e["status"] == "active" and sg["field"] in ("start_date", "probation_end"):
                engine.reschedule_future(c, e)
        audit(c, u["id"], f"suggestion_{action}", f"suggestion:{sid}")
    return {"ok": True}


@app.post("/api/employees/{emp_id}/invite")
def create_invite(emp_id: int, u: dict = Depends(hr_user)):
    token = new_invite_token()
    with tx() as c:
        _emp_or_404(c, emp_id)
        c.execute("UPDATE invites SET revoked_at=now() WHERE employee_id=%s AND used_at IS NULL AND revoked_at IS NULL", (emp_id,))
        c.execute("""INSERT INTO invites(kind, employee_id, token_hash, created_by, expires_at)
                     VALUES ('employee',%s,%s,%s,%s)""",
                  (emp_id, token_hash(token), u["id"], datetime.now(timezone.utc) + timedelta(days=14)))
        audit(c, u["id"], "create_invite", f"employee:{emp_id}")
    return {"link": f"{get_settings().max_bot_url}?start={token}", "expires_in_days": 14,
            "note": "Ссылка показывается один раз; храните её как пароль."}


@app.post("/api/invites/{inv_id}/revoke")
def revoke_invite(inv_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        c.execute("UPDATE invites SET revoked_at=now() WHERE id=%s AND revoked_at IS NULL", (inv_id,))
        audit(c, u["id"], "revoke_invite", f"invite:{inv_id}")
    return {"ok": True}


class EventPatch(BaseModel):
    due_at: datetime | None = None
    disabled: bool | None = None


@app.patch("/api/events/{ev_id}")
def patch_event(ev_id: int, body: EventPatch, u: dict = Depends(hr_user)):
    with tx() as c:
        ev = c.execute("SELECT * FROM events WHERE id=%s FOR UPDATE", (ev_id,)).fetchone()
        if not ev or ev["status"] != "scheduled":
            raise HTTPException(400, "Менять можно только запланированное событие")
        if body.due_at:
            c.execute("UPDATE events SET due_at=%s, updated_at=now() WHERE id=%s", (body.due_at, ev_id))
        if body.disabled is not None:
            c.execute("UPDATE events SET disabled=%s, updated_at=now() WHERE id=%s", (body.disabled, ev_id))
        c.execute("NOTIFY korsik_jobs")
        audit(c, u["id"], "patch_event", f"event:{ev_id}", body.model_dump(mode="json", exclude_none=True))
    return {"ok": True}


@app.post("/api/events/{ev_id}/permit")
def permit_event(ev_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        ev = c.execute("SELECT * FROM events WHERE id=%s FOR UPDATE", (ev_id,)).fetchone()
        if not ev or not ev["needs_permission"]:
            raise HTTPException(400, "Событие не требует разрешения")
        c.execute("UPDATE events SET permitted_at=COALESCE(permitted_at, now()), permitted_by=%s, updated_at=now() WHERE id=%s",
                  (u["id"], ev_id))
        c.execute("NOTIFY korsik_jobs")
        audit(c, u["id"], "permit_event", f"event:{ev_id}")
    return {"ok": True}


class TestSurveyIn(BaseModel):
    survey: str = "check2w"
    seconds: int = Field(45, ge=10, le=600)


@app.post("/api/employees/{emp_id}/test-survey")
def test_survey(emp_id: int, body: TestSurveyIn, u: dict = Depends(hr_user)):
    return schedule_test_survey(emp_id, body.survey, body.seconds, u["id"])


def schedule_test_survey(emp_id: int, survey: str, seconds: int, user_id: int | None) -> dict:
    if survey not in engine.scenario()["surveys"]:
        raise HTTPException(400, "Нет такого опроса")
    with tx() as c:
        e = c.execute("SELECT * FROM employees WHERE id=%s", (emp_id,)).fetchone()
        if not e or not e["max_chat_id"]:
            raise HTTPException(400, "Сотрудник не привязан к MAX")
        key = f"test:{survey}:{int(time.time() * 1000)}"
        ev = c.execute("""INSERT INTO events(employee_id, step_key, scenario_code, title, source_rows, due_at, is_test)
                          VALUES (%s,%s,%s,%s,'test',now() + make_interval(secs => %s), true) RETURNING id, due_at""",
                       (emp_id, key, engine.scenario()["code"], f"ТЕСТ: {engine.scenario()['surveys'][survey]['title']}",
                        seconds)).fetchone()
        audit(c, user_id, "test_survey", f"employee:{emp_id}", {"survey": survey})
    return {"event_id": ev["id"], "due_at": ev["due_at"]}


@app.post("/api/employees/{emp_id}/intro/approve")
def approve_intro(emp_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        c.execute("UPDATE employees SET intro_approved_at=now() WHERE id=%s", (emp_id,))
        audit(c, u["id"], "approve_intro", f"employee:{emp_id}")
    return {"ok": True, "note": "Отмечено как согласованное. Публикация во внешних каналах выполняется отдельно."}


@app.post("/api/employees/{emp_id}/meeting-request")
def meeting_request(emp_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        e = _emp_or_404(c, emp_id)
        if not e["manager_user_id"]:
            raise HTTPException(400, "У сотрудника не указан руководитель")
        cases_mod.manager_meeting(c, e, basis="запрос HR-специалиста по итогам адаптации",
                                  dedupe=f"meeting:hr:{emp_id}:{date.today().isoformat()}")
    return {"ok": True}


# ---------------------------------------------------------------- materials
@app.get("/api/scenario")
def get_scenario(u: dict = Depends(hr_user)):
    sc = engine.scenario()
    with tx() as c:
        mats = c.execute("SELECT id, key, title, tags, kind, filename, url, employee_id, created_at FROM materials WHERE active").fetchall()
    have: dict[str, list] = {}
    for m in mats:
        have.setdefault(m["key"], []).append(m)
    slots = [{**m, "uploaded": [x for x in have.get(m["key"], []) if x["employee_id"] is None]} for m in sc["materials"]]
    steps = [{"key": s["key"], "rows": s["rows"], "title": s["title"], "anchor": s["anchor"], "tags": s.get("tags"),
              "work_format": s.get("work_format"), "needs_permission": s.get("needs_permission", False),
              "permission_note": s.get("permission_note")} for s in sc["steps"]]
    return {"code": sc["code"], "source": sc["source"], "steps": steps, "materials": slots,
            "ambiguities": sc["ambiguities"], "surveys": {k: {"title": v["title"], "rows": v["rows"],
                                                              "questions": len(v["questions"])} for k, v in sc["surveys"].items()}}


MAT_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".pptx", ".docx", ".xlsx"}


@app.post("/api/materials")
def upload_material(key: str = Form(...), title: str = Form(""), tags: str = Form(""), kind: str = Form("file"),
                    url: str = Form(""), body: str = Form(""), employee_id: int | None = Form(None),
                    file: UploadFile | None = File(None), u: dict = Depends(hr_user)):
    valid = {m["key"] for m in engine.scenario()["materials"]}
    if key not in valid:
        raise HTTPException(400, "Неизвестный слот материала")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if set(tag_list) - TAGS:
        raise HTTPException(400, "Неизвестный тег")
    stored = fname = None
    if kind in ("file", "image"):
        if not file:
            raise HTTPException(400, "Нужен файл")
        stored, _, _ = _safe_store(file, get_settings().data_dir / "materials", MAT_EXT, 50 * 1024 * 1024)
        fname = Path(file.filename).name
        if Path(fname).suffix.lower() in (".png", ".jpg", ".jpeg"):
            kind = "image"
    elif kind == "link":
        if not re.match(r"^https?://", url):
            raise HTTPException(400, "Нужна ссылка http(s)")
    elif kind != "text":
        raise HTTPException(400)
    slot = next(m for m in engine.scenario()["materials"] if m["key"] == key)
    with tx() as c:
        if employee_id:
            _emp_or_404(c, employee_id)
        row = c.execute("""INSERT INTO materials(key, title, tags, employee_id, kind, filename, stored_name, url, body, uploaded_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (key, title or slot["title"], tag_list, employee_id, kind, fname, stored, url or None,
                         body or None, u["id"])).fetchone()
        audit(c, u["id"], "upload_material", f"material:{row['id']}", {"key": key})
    return {"id": row["id"]}


@app.delete("/api/materials/{mid}")
def delete_material(mid: int, u: dict = Depends(hr_user)):
    with tx() as c:
        c.execute("UPDATE materials SET active=false WHERE id=%s", (mid,))
        audit(c, u["id"], "deactivate_material", f"material:{mid}")
    return {"ok": True}


# ---------------------------------------------------------------- cases / dialog
@app.get("/api/cases")
def list_cases(status: str = "", u: dict = Depends(hr_user)):
    with tx() as c:
        rows = c.execute("""SELECT x.*, e.full_name, e.dialog_mode, e.department,
                              (SELECT count(*) FROM messages m WHERE m.case_id=x.id) AS messages
                            FROM cases x JOIN employees e ON e.id=x.employee_id
                            WHERE (%s = '' OR x.status=%s)
                            ORDER BY CASE x.status WHEN 'new' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                                     CASE x.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, x.id DESC""",
                         (status, status)).fetchall()
    return rows


@app.get("/api/cases/{case_id}")
def get_case(case_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        x = c.execute("SELECT * FROM cases WHERE id=%s", (case_id,)).fetchone()
        if not x:
            raise HTTPException(404)
        e = c.execute("SELECT * FROM employees WHERE id=%s", (x["employee_id"],)).fetchone()
        hist = c.execute("""SELECT m.id, m.direction, m.author_kind, m.text, m.created_at, m.case_id,
                                   o.status AS delivery, o.last_error AS delivery_error, us.display_name AS author
                            FROM messages m LEFT JOIN outbox o ON o.message_id=m.id LEFT JOIN users us ON us.id=m.author_user_id
                            WHERE m.employee_id=%s AND (m.case_id=%s OR m.id >= COALESCE(%s, 0) - 6)
                            ORDER BY m.id DESC LIMIT 60""", (x["employee_id"], case_id, x["root_message_id"])).fetchall()
        an = c.execute("SELECT * FROM analyses WHERE employee_id=%s ORDER BY id DESC LIMIT 5", (x["employee_id"],)).fetchall()
        stage = analytics.stage_of(e, datetime.now(timezone.utc).date())
    return {"case": x, "employee": _emp_public(e), "stage": stage, "history": list(reversed(hist)), "analyses": an,
            "reason_label": cases_mod.REASON_LABELS.get(x["reason"]), "topic_label": cases_mod.TOPIC_LABELS.get(x["topic"])}


@app.post("/api/cases/{case_id}/take")
def take_case(case_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        try:
            cases_mod.take(c, u, case_id)
        except LookupError:
            raise HTTPException(404)
    return {"ok": True}


class ReplyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@app.post("/api/employees/{emp_id}/reply")
def reply(emp_id: int, body: ReplyIn, u: dict = Depends(hr_user)):
    with tx() as c:
        try:
            ob = cases_mod.hr_reply(c, u, emp_id, body.text)
        except PermissionError as e:
            raise HTTPException(400, str(e))
    return {"outbox_id": ob}


class ReleaseIn(BaseModel):
    resolve: bool = True


@app.post("/api/employees/{emp_id}/release")
def release(emp_id: int, body: ReleaseIn, u: dict = Depends(hr_user)):
    with tx() as c:
        try:
            mode = cases_mod.release(c, u, emp_id, resolve_case=body.resolve)
        except PermissionError as e:
            raise HTTPException(400, str(e))
    return {"mode": mode}


@app.post("/api/cases/{case_id}/resolve")
def resolve_case(case_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        x = c.execute("SELECT x.*, e.dialog_mode FROM cases x JOIN employees e ON e.id=x.employee_id WHERE x.id=%s",
                      (case_id,)).fetchone()
        if not x:
            raise HTTPException(404)
        if x["dialog_mode"] == "hr_active":
            raise HTTPException(400, "Диалог у HR: завершите участие кнопкой «Вернуть Корсику»")
        cases_mod.resolve(c, u, case_id)
    return {"ok": True}


@app.get("/api/delivery")
def delivery_problems(u: dict = Depends(hr_user)):
    with tx() as c:
        return c.execute("""SELECT o.id, o.status, o.last_error, o.attempts, o.created_at, o.source, o.employee_id,
                                   e.full_name, left(o.body->>'text', 160) AS text
                            FROM outbox o LEFT JOIN employees e ON e.id=o.employee_id
                            WHERE o.status IN ('uncertain','failed') ORDER BY o.id DESC LIMIT 200""").fetchall()


@app.post("/api/delivery/{ob_id}/retry")
def retry_delivery(ob_id: int, u: dict = Depends(hr_user)):
    """Explicit HR decision: resend an uncertain/failed message (may produce a duplicate)."""
    with tx() as c:
        c.execute("""UPDATE outbox SET status='pending', next_attempt_at=now(), last_error='manual_retry'
                     WHERE id=%s AND status IN ('uncertain','failed')""", (ob_id,))
        c.execute("NOTIFY korsik_jobs")
        audit(c, u["id"], "retry_delivery", f"outbox:{ob_id}")
    return {"ok": True}


@app.post("/api/delivery/{ob_id}/dismiss")
def dismiss_delivery(ob_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        c.execute("UPDATE outbox SET status='cancelled', last_error=coalesce(last_error,'')||' | dismissed' WHERE id=%s AND status IN ('uncertain','failed')", (ob_id,))
        audit(c, u["id"], "dismiss_delivery", f"outbox:{ob_id}")
    return {"ok": True}


@app.get("/api/attention")
def attention(u: dict = Depends(hr_user)):
    with tx() as c:
        return c.execute("""SELECT a.id, a.employee_id, e.full_name, r.survey_key, a.question_text, a.qtype,
                                   a.option_labels, a.score, a.text, a.comment, a.updated_at,
                                   an.sentiment, an.priority, an.summary
                            FROM survey_answers a JOIN survey_runs r ON r.id=a.run_id JOIN employees e ON e.id=a.employee_id
                            LEFT JOIN analyses an ON an.source_type='survey_answer' AND an.source_id=a.id
                            WHERE (a.score IS NOT NULL AND a.score < 8) OR 'no' = ANY(a.option_ids)
                               OR a.option_ids && ARRAY['c','nobody','b']::text[] OR an.priority IN ('medium','high')
                            ORDER BY a.updated_at DESC LIMIT 200""").fetchall()


# ---------------------------------------------------------------- corpus
@app.get("/api/corpus")
def corpus_list(u: dict = Depends(hr_user)):
    with tx() as c:
        docs = c.execute("""SELECT id, title, filename, version_label, organizations, tags, effective_from, replaces_id,
                                   status, error, published_at, created_at,
                                   jsonb_array_length(coalesce(pages,'[]')) AS pages_count
                            FROM corpus_documents ORDER BY id DESC""").fetchall()
        vers = c.execute("""SELECT v.id, v.status, v.document_ids, v.dense, v.error, v.reason, v.created_at, v.activated_at,
                                   (SELECT count(*) FROM chunks ch WHERE ch.corpus_version_id=v.id) chunks
                            FROM corpus_versions v ORDER BY id DESC LIMIT 20""").fetchall()
    return {"documents": docs, "versions": vers}


@app.post("/api/corpus")
def corpus_upload(file: UploadFile = File(...), title: str = Form(...), organizations: str = Form(...),
                  tags: str = Form(""), version_label: str = Form("1"), effective_from: str = Form(""),
                  replaces_id: int | None = Form(None), u: dict = Depends(hr_user)):
    orgs = [o.strip() for o in organizations.split(";") if o.strip()]
    if not orgs:
        raise HTTPException(400, "Укажите применимость: организацию(и) или * для всей группы")
    name, size, digest = _safe_store(file, get_settings().corpus_dir, corpus_mod.ALLOWED_EXT, corpus_mod.MAX_BYTES)
    with tx() as c:
        d = c.execute("""INSERT INTO corpus_documents(title, filename, stored_name, sha256, version_label, organizations,
                         tags, effective_from, replaces_id, uploaded_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                      (title.strip(), Path(file.filename).name, name, digest, version_label, orgs,
                       [t.strip() for t in tags.split(",") if t.strip()], effective_from or None, replaces_id,
                       u["id"])).fetchone()
        enqueue(c, "corpus_extract", {"document_id": d["id"]}, dedupe_key=f"corpus_extract:{d['id']}")
        audit(c, u["id"], "corpus_upload", f"corpus_document:{d['id']}")
    return {"id": d["id"]}


@app.get("/api/corpus/search")
def corpus_search(q: str, employee_id: int | None = None, u: dict = Depends(hr_user)):
    from ..rag.index import search
    with tx() as c:
        emp = c.execute("SELECT * FROM employees WHERE id=%s", (employee_id,)).fetchone() if employee_id else None
    r = search(q, emp, use_dense=True)
    return {"version_id": r["version_id"], "dense_used": r.get("dense_used"), "dense_error": r.get("dense_error"),
            "hits": [{k: h[k] for k in ("title", "page", "unit", "text", "document_id")} for h in r["hits"][:8]]}


@app.get("/api/corpus/{doc_id}")
def corpus_doc(doc_id: int, u: dict = Depends(hr_user)):
    with tx() as c:
        d = c.execute("SELECT * FROM corpus_documents WHERE id=%s", (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404)
    return d


@app.post("/api/corpus/{doc_id}/{action}")
def corpus_action(doc_id: int, action: str, u: dict = Depends(hr_user)):
    with tx() as c:
        if action == "publish":
            try:
                corpus_mod.request_publish(c, doc_id, u["id"])
            except ValueError as e:
                raise HTTPException(400, str(e))
        elif action == "withdraw":
            corpus_mod.request_withdraw(c, doc_id, u["id"])
        else:
            raise HTTPException(400)
        audit(c, u["id"], f"corpus_{action}", f"corpus_document:{doc_id}")
    return {"ok": True}


# ---------------------------------------------------------------- analytics / digest / notifications
@app.get("/api/analytics")
def get_analytics(days: int = 30, department: str = "", tag: str = "", u: dict = Depends(hr_user)):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    with tx() as c:
        ids = None
        if department or tag:
            ids = [r["id"] for r in c.execute("""SELECT id FROM employees WHERE (%s='' OR department=%s)
                                                 AND (%s='' OR %s = ANY(tags))""", (department, department, tag, tag)).fetchall()]
        cur = analytics.compute(c, start, end, employee_ids=ids)
        prev = analytics.compute(c, start - timedelta(days=days), start, employee_ids=ids)
        weekly = []
        for w in range(8, 0, -1):
            ws, we = end - timedelta(days=7 * w), end - timedelta(days=7 * (w - 1))
            row = c.execute("""SELECT sentiment, count(*) n FROM analyses WHERE analyzer_version=%s AND created_at>=%s
                               AND created_at<%s AND (%s::bigint[] IS NULL OR employee_id = ANY(%s)) GROUP BY sentiment""",
                            (analytics.VERSION, ws, we, ids, ids)).fetchall()
            weekly.append({"week_start": ws.date().isoformat(), **{r["sentiment"]: r["n"] for r in row}})
        deps = [r["department"] for r in c.execute("SELECT DISTINCT department FROM employees WHERE department<>'' ORDER BY 1").fetchall()]
    return {"current": cur, "previous": prev, "weekly": weekly, "departments": deps}


@app.get("/api/notifications")
def notifications(u: dict = Depends(current_user)):
    with tx() as c:
        return c.execute("""SELECT id, kind, title, body, reason, channel_status, read_at, created_at, employee_id, case_id
                            FROM notifications WHERE recipient_user_id=%s ORDER BY id DESC LIMIT 100""", (u["id"],)).fetchall()


@app.post("/api/notifications/{nid}/read")
def read_notification(nid: int, u: dict = Depends(current_user)):
    with tx() as c:
        c.execute("UPDATE notifications SET read_at=now() WHERE id=%s AND recipient_user_id=%s", (nid, u["id"]))
    return {"ok": True}


@app.get("/api/digests")
def digests(u: dict = Depends(current_user)):
    if not u["is_hr_head"]:
        raise HTTPException(403, "Нет прав руководителя HR-направления")
    with tx() as c:
        return c.execute("SELECT * FROM digests WHERE recipient_user_id=%s ORDER BY period_start DESC LIMIT 20",
                         (u["id"],)).fetchall()


@app.post("/api/digests/run")
def run_digest(u: dict = Depends(current_user)):
    if not u["is_hr_head"]:
        raise HTTPException(403)
    with tx() as c:
        data = analytics.weekly(c, u)
    return {"created": data is not None}


# ---------------------------------------------------------------- users (managers)
class UserIn(BaseModel):
    login: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]{3,40}$")
    password: str = Field(..., min_length=10)
    display_name: str


@app.get("/api/users")
def users(u: dict = Depends(hr_user)):
    with tx() as c:
        return c.execute("""SELECT u.id, u.login, u.role, u.is_hr_head, u.display_name, u.active,
                                   u.max_linked_at IS NOT NULL AS max_linked,
                                   (SELECT count(*) FROM employees e WHERE e.manager_user_id=u.id) AS team
                            FROM users u ORDER BY u.role, u.display_name""").fetchall()


@app.post("/api/users")
def create_manager(body: UserIn, u: dict = Depends(hr_user)):
    from ..security import hash_password
    with tx() as c:
        try:
            row = c.execute("""INSERT INTO users(login, password_hash, role, display_name) VALUES (%s,%s,'manager',%s)
                               RETURNING id""", (body.login, hash_password(body.password), body.display_name)).fetchone()
        except Exception:
            raise HTTPException(400, "Логин занят или пароль слишком простой")
        audit(c, u["id"], "create_manager", f"user:{row['id']}")
    return row


@app.post("/api/users/{uid}/invite")
def user_invite(uid: int, u: dict = Depends(hr_user)):
    token = new_invite_token()
    with tx() as c:
        if not c.execute("SELECT id FROM users WHERE id=%s", (uid,)).fetchone():
            raise HTTPException(404)
        c.execute("""INSERT INTO invites(kind, user_id, token_hash, created_by, expires_at) VALUES ('manager',%s,%s,%s,%s)""",
                  (uid, token_hash(token), u["id"], datetime.now(timezone.utc) + timedelta(days=7)))
        audit(c, u["id"], "user_invite", f"user:{uid}")
    return {"link": f"{get_settings().max_bot_url}?start={token}"}


@app.post("/api/users/{uid}/active/{flag}")
def user_active(uid: int, flag: bool, u: dict = Depends(hr_user)):
    with tx() as c:
        c.execute("UPDATE users SET active=%s WHERE id=%s AND role='manager'", (flag, uid))
        if not flag:
            c.execute("DELETE FROM sessions WHERE user_id=%s", (uid,))
    return {"ok": True}


# ---------------------------------------------------------------- manager view (restricted)
@app.get("/api/manager/team")
def manager_team(u: dict = Depends(current_user)):
    if u["role"] != "manager":
        raise HTTPException(403)
    today = datetime.now(timezone.utc).date()
    with tx() as c:
        team = c.execute("SELECT * FROM employees WHERE manager_user_id=%s AND status<>'deactivated' ORDER BY full_name",
                         (u["id"],)).fetchall()
        out = []
        for e in team:
            nxt = c.execute("""SELECT title, due_at FROM events WHERE employee_id=%s AND status='scheduled' AND NOT is_test
                               ORDER BY due_at LIMIT 3""", (e["id"],)).fetchall()
            out.append({"id": e["id"], "full_name": e["full_name"], "position": e["position"],
                        "department": e["department"], "start_date": e["start_date"],
                        "probation_end": engine.effective_probation_end(e) if e["start_date"] else None,
                        "status": e["status"], "stage": analytics.stage_of(e, today),
                        "progress": analytics.progress(c, e["id"]), "next": nxt, "max_linked": bool(e["max_user_id"])})
        notes = c.execute("""SELECT id, title, body, reason, channel_status, read_at, created_at FROM notifications
                             WHERE recipient_user_id=%s ORDER BY id DESC LIMIT 50""", (u["id"],)).fetchall()
    return {"team": out, "notifications": notes}


# ---------------------------------------------------------------- frontend
@app.get("/{path:path}")
def spa(path: str):
    if path.startswith(("api/", "webhook/")):
        raise HTTPException(404)
    f = FRONT / path
    if path and f.is_file() and FRONT in f.resolve().parents:
        return FileResponse(f)
    index = FRONT / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "frontend not built"}, status_code=404)
