"""Outgoing messages: persisted first (messages + outbox), delivered by the sender.

All employee-facing messages go through one adapter on behalf of Корсик. Internally we
keep who produced the text (bot_script / bot_llm / bot_system / hr) and the dialog
revision: automatic messages are cancelled if the dialog moved on (HR took it over).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import get_settings
from .db import J, tx
from .transport import Rejected, Uncertain, build_body, get_transport

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 6

AUTO_SOURCES = {"scenario", "llm", "system"}


def personalize(emp: dict, text: str, text_vy: str | None = None) -> str:
    use = text_vy if (emp.get("address_form") == "vy" and text_vy) else text
    name = (emp.get("short_name") or "").strip() or (emp.get("full_name") or "").split(" ")[-1] or ""
    if not name:
        use = use.replace(", {name}", "").replace(" {name}", "").replace("{name}, ", "").replace("{name}", "")
    return use.replace("{name}", name)


def queue_to_employee(conn, emp: dict, text: str, *, author_kind: str, source: str,
                      buttons: list | None = None, material_ids: list[int] | None = None,
                      case_id: int | None = None, author_user_id: int | None = None,
                      survey_run_id: int | None = None, corpus_version_id: int | None = None,
                      cited_doc_ids: list[int] | None = None, request_id: str | None = None,
                      edit_of_outbox: int | None = None, payload: dict | None = None) -> int:
    """Persist message + outbox row in the caller's transaction. Returns outbox id."""
    msg = conn.execute(
        """INSERT INTO messages(employee_id, direction, author_kind, author_user_id, text, payload,
                                case_id, dialog_mode, dialog_revision, request_id)
           VALUES (%s,'out',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (emp["id"], author_kind, author_user_id, text, J(payload or {"buttons": buttons or []}),
         case_id, emp["dialog_mode"], emp["dialog_revision"], request_id)).fetchone()
    body = {"text": text, "buttons": buttons or [], "materials": material_ids or []}
    if edit_of_outbox:
        body["edit_of_outbox"] = edit_of_outbox
    row = conn.execute(
        """INSERT INTO outbox(message_id, employee_id, chat_id, body, source, requires_revision,
                              corpus_version_id, cited_doc_ids, survey_run_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (msg["id"], emp["id"], emp.get("max_chat_id"), J(body), source,
         emp["dialog_revision"] if source in AUTO_SOURCES else None,
         corpus_version_id, cited_doc_ids or [], survey_run_id)).fetchone()
    conn.execute("NOTIFY korsik_jobs")
    return row["id"]


def queue_to_user(conn, user: dict, text: str, *, buttons: list | None = None) -> int | None:
    """Service message to HR / manager MAX chat (only if their MAX link is confirmed)."""
    if not user.get("max_chat_id") or not user.get("max_linked_at"):
        return None
    msg = conn.execute(
        "INSERT INTO messages(user_id, direction, author_kind, text) VALUES (%s,'out','bot_system',%s) RETURNING id",
        (user["id"], text)).fetchone()
    row = conn.execute(
        "INSERT INTO outbox(message_id, user_id, chat_id, body, source) VALUES (%s,%s,%s,%s,'service') RETURNING id",
        (msg["id"], user["id"], user["max_chat_id"], J({"text": text, "buttons": buttons or []}))).fetchone()
    return row["id"]


# ---------------------------------------------------------------- sender
def _claim(limit: int = 20) -> list[dict]:
    with tx() as c:
        rows = c.execute(
            """SELECT * FROM outbox WHERE status='pending' AND next_attempt_at <= now()
               ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", (limit,)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute("UPDATE outbox SET status='sending', attempts=attempts+1 WHERE id = ANY(%s)", (ids,))
    return rows


def _still_allowed(c, ob: dict) -> str | None:
    """Final right-to-send check right before the network call. Returns cancel reason."""
    if ob["employee_id"] is None:
        return None
    emp = c.execute("SELECT dialog_mode, dialog_revision, max_chat_id, status FROM employees WHERE id=%s",
                    (ob["employee_id"],)).fetchone()
    if not emp or not emp["max_chat_id"]:
        return "no_chat"
    if emp["status"] == "deactivated":
        return "deactivated"
    if emp["status"] == "paused" and ob["source"] in AUTO_SOURCES:
        return "paused"
    if ob["source"] in AUTO_SOURCES:
        if emp["dialog_mode"] == "hr_active":
            return "hr_active"
        if ob["requires_revision"] is not None and emp["dialog_revision"] != ob["requires_revision"]:
            return "stale_revision"
    if ob["source"] == "llm" and ob["cited_doc_ids"]:
        bad = c.execute(
            "SELECT count(*) AS n FROM corpus_documents WHERE id = ANY(%s) AND status <> 'published'",
            (ob["cited_doc_ids"],)).fetchone()["n"]
        if bad:
            return "source_withdrawn"
    return None


def _attachments(transport, material_ids: list[int]) -> tuple[list[dict], list[str]]:
    s = get_settings()
    out, missing = [], []
    for mid in material_ids:
        with tx() as c:
            m = c.execute("SELECT * FROM materials WHERE id=%s AND active", (mid,)).fetchone()
        if not m:
            missing.append(str(mid))
            continue
        if m["kind"] in ("link", "text"):
            continue  # rendered into text when queued
        token = m["max_token"]
        kind = "image" if m["kind"] == "image" else "file"
        if not token or (transport.name == "console") != token.startswith("console-"):
            token = transport.upload(s.data_dir / "materials" / m["stored_name"], kind)
            with tx() as c:
                c.execute("UPDATE materials SET max_token=%s WHERE id=%s", (token, mid))
        out.append({"type": kind, "payload": {"token": token}})
    return out, missing


def send_pending(limit: int = 20) -> int:
    rows = _claim(limit)
    if not rows:
        return 0
    transport = get_transport()
    for ob in rows:
        with tx() as c:
            reason = _still_allowed(c, ob)
            if reason:
                c.execute("UPDATE outbox SET status='cancelled', last_error=%s WHERE id=%s", (reason, ob["id"]))
                if reason == "source_withdrawn":
                    from .dialog import requeue_after_withdrawal
                    requeue_after_withdrawal(c, ob)
                continue
            chat_id = ob["chat_id"]
            if ob["employee_id"]:
                chat_id = c.execute("SELECT max_chat_id FROM employees WHERE id=%s",
                                    (ob["employee_id"],)).fetchone()["max_chat_id"]
            edit_target = None
            if ob["body"].get("edit_of_outbox"):
                t = c.execute("SELECT external_id FROM outbox WHERE id=%s", (ob["body"]["edit_of_outbox"],)).fetchone()
                edit_target = t and t["external_id"]
        try:
            atts, missing = _attachments(transport, ob["body"].get("materials") or [])
            body = build_body(ob["body"], atts)
            if edit_target:
                transport.edit(edit_target, body)
                result_id = edit_target
            else:
                result_id = transport.send(chat_id, body).external_id
        except Rejected as e:
            retry = e.retryable and ob["attempts"] < MAX_ATTEMPTS
            with tx() as c:
                c.execute("""UPDATE outbox SET status=%s, last_error=%s, next_attempt_at=%s WHERE id=%s""",
                          ("pending" if retry else "failed", e.code,
                           datetime.now(timezone.utc) + timedelta(seconds=min(300, 5 * 2 ** ob["attempts"])),
                           ob["id"]))
                if not retry:
                    c.execute("UPDATE notifications SET channel_status='failed' WHERE outbox_id=%s", (ob["id"],))
            log.warning("outbox %s rejected %s retry=%s", ob["id"], e.code, retry)
            continue
        except Uncertain as e:
            with tx() as c:
                c.execute("UPDATE outbox SET status='uncertain', last_error=%s WHERE id=%s", (str(e), ob["id"]))
            log.warning("outbox %s uncertain %s", ob["id"], e)
            continue
        except Exception as e:  # local failure (file missing...) — not a delivery attempt
            with tx() as c:
                c.execute("UPDATE outbox SET status='failed', last_error=%s WHERE id=%s",
                          (f"local:{type(e).__name__}", ob["id"]))
            log.exception("outbox %s local failure", ob["id"])
            continue
        with tx() as c:
            c.execute("UPDATE outbox SET status='sent', external_id=%s, sent_at=now(), last_error=%s WHERE id=%s",
                      (result_id, ("missing_material:" + ",".join(missing)) if missing else None, ob["id"]))
            if ob["message_id"]:
                c.execute("UPDATE messages SET provider_message_id=%s WHERE id=%s", (result_id, ob["message_id"]))
            if ob["survey_run_id"]:
                from .scenario.survey import on_opening_accepted
                on_opening_accepted(c, ob["survey_run_id"])
            c.execute("UPDATE notifications SET channel_status='sent' WHERE outbox_id=%s", (ob["id"],))
    return len(rows)


def fail_stuck_sending() -> None:
    """After a crash, 'sending' rows are ambiguous: mark uncertain, never resend blindly."""
    with tx() as c:
        c.execute("""UPDATE outbox SET status='uncertain', last_error='crash_during_send'
                     WHERE status='sending'""")
