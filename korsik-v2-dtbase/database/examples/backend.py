"""SQLAlchemy 2 projects can use these operations through engine.begin()/exec_driver_sql.
This reference uses psycopg 3 directly to make transaction boundaries explicit.
Not a MAX SDK: verify the real webhook protocol before integration.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def new_invitation(conn, authenticated_hr_id: int, employee_id: int, expires_at: datetime):
    """Return the secret exactly once. Do not log it or put PII in it."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).digest()
    with conn.transaction():
        employee = conn.execute(
            "SELECT id FROM korsik.employees WHERE id=%s AND hr_id=%s AND lifecycle='invited' FOR UPDATE",
            (employee_id, authenticated_hr_id),
        ).fetchone()
        if not employee:
            raise PermissionError("Employee not assigned to this HR or already activated")
        conn.execute(
            "UPDATE korsik.invitations SET revoked_at=clock_timestamp() "
            "WHERE employee_id=%s AND revoked_at IS NULL AND activated_at IS NULL", (employee_id,)
        )
        conn.execute(
            "INSERT INTO korsik.invitations(employee_id,token_hash,expires_at) VALUES(%s,%s,%s)",
            (employee_id, digest, expires_at),
        )
        conn.execute(
            "INSERT INTO korsik.audit_log(employee_id,actor_hr_id,action) VALUES(%s,%s,'invitation_issued')",
            (employee_id, authenticated_hr_id),
        )
    return token  # Build the actual MAX deep link using documented MAX syntax.


def receive_event(conn, provider_event_id: str, normalized_envelope: dict):
    """Call only after authenticating the webhook.

    The MAX adapter must construct an allowlisted envelope. In particular replace
    a /start invitation secret with invitation_hash BEFORE this function; do not
    retain the raw secret in body, nested objects, application logs or tracing.
    No arbitrary user-supplied HR ID, employee ID or pretrusted routing action.
    """
    with conn.transaction():
        inserted = conn.execute(
            "INSERT INTO korsik.max_inbox(event_id,payload) VALUES(%s,%s) "
            "ON CONFLICT(event_id) DO NOTHING RETURNING event_id",
            (provider_event_id, Jsonb(normalized_envelope)),
        ).fetchone()
    # HTTP 200 only after COMMIT, including duplicates; a worker handles processing.
    return inserted is not None


def claim_inbox(conn):
    with conn.transaction():
        return conn.execute("""
            WITH picked AS (
              SELECT event_id FROM korsik.max_inbox
              WHERE status='pending' AND next_attempt_at<=clock_timestamp()
              ORDER BY next_attempt_at,event_id LIMIT 1 FOR UPDATE SKIP LOCKED
            ) UPDATE korsik.max_inbox i SET status='processing',attempts=i.attempts+1,
              generation=i.generation+1,lease_until=clock_timestamp()+interval '1 minute'
              FROM picked WHERE i.event_id=picked.event_id RETURNING i.*
            """).fetchone()


def sign_callback(employee_id: int, target_id: int, action: str, secret: bytes) -> str:
    """MAX adapter must also check the actual callback size limit.
    target_id is the immutable outgoing PROMPT MESSAGE id, not the plan event id.
    Read its payload to recover event/question/revision, then validate via DB operation.
    Database IDs are not authorization. HMAC binds a button to one employee/action.
    """
    if ":" in action or not action or len(action) > 100:
        raise ValueError("Invalid option/action id")
    body = f"{employee_id}:{target_id}:{action}"
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + ":" + sig


def verify_callback(value: str, employee_id: int, secret: bytes):
    eid, target, action, sig = value.split(":")
    expected = sign_callback(int(eid), int(target), action, secret)
    if int(eid) != employee_id or not hmac.compare_digest(value, expected):
        raise PermissionError("Invalid callback")
    return int(target), action



def survey_value_valid(question: dict, answer_type: str, value, status: str) -> bool:
    """Friendly input validation; DB remains the authority. No semantic guessing."""
    if answer_type == "comment":
        return status == "awaiting_comment" and isinstance(value,str) and 1 <= len(value.strip()) <= 8000
    if status != "awaiting_answer":
        return False
    if answer_type == "skip":
        return not question.get("required",True)
    if answer_type != question["type"]:
        return False
    if answer_type == "rating":
        return type(value) is int and 1 <= value <= 10
    if answer_type == "boolean":
        return type(value) is bool
    if answer_type == "text":
        return isinstance(value,str) and 1 <= len(value.strip()) <= 8000
    ids = {o["id"] for o in question.get("options",[])}
    if answer_type == "single_choice":
        return isinstance(value,str) and value in ids
    if answer_type == "multi_choice":
        return (isinstance(value,list) and all(isinstance(x,str) for x in value)
                and 1 <= len(value) == len(set(value)) <= 30 and set(value) <= ids
                and not (len(value)>1 and set(value) & set(question.get("exclusive_options",[]))))
    if answer_type == "attachment":
        return (isinstance(value,dict) and set(value)=={"asset_ref","kind"}
                and isinstance(value["asset_ref"],str) and 1 <= len(value["asset_ref"]) <= 500
                and value["kind"] in {"photo","image","pdf","file"})
    return False

@dataclass(frozen=True)
class Action:
    # Produced by trusted backend routing, never deserialize directly from webhook.
    kind: str
    value: str = ""
    event_id: int | None = None
    case_id: int | None = None
    revision: int | None = None
    field: str | None = None
    question_code: str | None = None
    interaction_revision: int | None = None
    answer_type: str | None = None
    json_value: object = None
    context_revision: int | None = None


def process_claim(conn, claim: dict, route: Callable[[dict, dict], Action]):
    """No network/LLM calls inside route() or this transaction.
    route distinguishes questions from explicit form/event input. Awaiting a
    rating does NOT convert arbitrary text into a rating. Comments need an
    explicit reply context or a signed 'comment for event X' interaction.
    """
    try:
        with conn.transaction():
            inbox = conn.execute(
                "SELECT * FROM korsik.max_inbox WHERE event_id=%s FOR UPDATE", (claim["event_id"],)
            ).fetchone()
            if inbox["status"] != "processing" or inbox["generation"] != claim["generation"]:
                raise RuntimeError("Stale inbox lease")
            payload = inbox["payload"]
            if payload.get("type") == "delivery":
                conn.execute("SELECT korsik.confirm_delivery(%s,%s)",
                             (payload["external_message_id"], payload["delivered_at"]))
                result = {"route": "delivery"}
            else:
                if payload.get("type") == "invitation":
                    eid = conn.execute("SELECT korsik.activate_invitation(%s,%s,%s) AS id",
                        (bytes.fromhex(payload["invitation_hash"]),payload["account_id"],payload["chat_id"])
                    ).fetchone()["id"]
                    result = {"route": "activated", "employee_id": eid}
                    conn.execute("UPDATE korsik.max_inbox SET employee_id=%s WHERE event_id=%s", (eid,inbox["event_id"]))
                else:
                    employee = conn.execute(
                        "SELECT * FROM korsik.employees WHERE max_account_id=%s AND max_chat_id=%s FOR UPDATE",
                        (payload["account_id"], payload["chat_id"]),
                    ).fetchone()
                    if not employee:
                        raise PermissionError("Account/chat is not activated")
                    eid = employee["id"]
                    conn.execute("SELECT set_config('korsik.analysis_model_version',%s,true)",
                                 (os.getenv("HR_ANALYSIS_MODEL_VERSION","unconfigured"),))
                    conn.execute("SELECT set_config('korsik.analysis_policy_version',%s,true)",
                                 (os.getenv("HR_ANALYSIS_POLICY_VERSION","unconfigured"),))
                    action = route(employee,payload)
                    if action.kind in {"survey_answer","survey_toggle"}:
                        target = conn.execute("SELECT definition,current_question_code,interaction_revision,status,"
                            "deadline_at>clock_timestamp() AS timely FROM korsik.plan_events "
                            "WHERE id=%s AND employee_id=%s",(action.event_id,eid)).fetchone()
                        if (not target or target["status"] not in {"awaiting_answer","awaiting_comment"}
                            or not target["timely"] or target["current_question_code"] != action.question_code
                            or target["interaction_revision"] != action.interaction_revision):
                            action = Action("stale_action")
                        elif action.kind == "survey_answer":
                            question = next(q for q in target["definition"]["questions"] if q["code"] == action.question_code)
                            if not survey_value_valid(question,action.answer_type,action.json_value,target["status"]):
                                action = Action("invalid_answer")
                    if action.kind == "registration":
                        if employee["lifecycle"] != "registering" or employee["registration_revision"] != action.revision:
                            action = Action("stale_action")
                        elif ((action.field == "full_name" and not action.value.strip()) or
                              (action.field == "phone" and not re.fullmatch(r"\+[1-9][0-9]{7,14}",action.value))):
                            action = Action("invalid_answer")
                    msg = conn.execute("""INSERT INTO korsik.messages
                       (employee_id,direction,author,body,inbox_event_id,event_id,case_id,payload)
                       VALUES(%s,'in','employee',%s,%s,%s,%s,%s) RETURNING id""",
                       (eid,payload.get("body", ""),inbox["event_id"],action.event_id,action.case_id,Jsonb(payload.get("message_payload", {})))
                    ).fetchone()["id"]
                    conn.execute("SELECT set_config('korsik.source_message_id',%s,true)",(str(msg),))
                    result = {"route":action.kind,"message_id":msg}
                    if action.kind == "registration":
                        revision = conn.execute("SELECT korsik.save_registration(%s,%s,%s,%s) AS revision",
                            (eid,action.revision,action.field,action.value)).fetchone()["revision"]
                        result["revision"] = revision
                    elif action.kind == "survey_answer":
                        result["status"] = conn.execute(
                            "SELECT korsik.answer_survey_question(%s,%s,%s,%s,%s,%s,%s) AS status",
                            (eid,action.event_id,action.question_code,action.answer_type,
                             Jsonb(action.json_value),action.interaction_revision,msg)).fetchone()["status"]
                    elif action.kind == "survey_toggle":
                        conn.execute("SELECT korsik.toggle_survey_option(%s,%s,%s,%s,%s)",
                            (eid,action.event_id,action.question_code,action.value,action.interaction_revision))
                    elif action.kind == "preference_address":
                        conn.execute("SELECT korsik.set_communication_style(%s,%s,%s)",
                            (eid,action.value,action.context_revision))
                    elif action.kind in {"rating","comment","task_confirm","feedback"}:
                        result["status"] = conn.execute("SELECT korsik.answer_event(%s,%s,%s,%s) AS status",
                            (eid,action.event_id,action.kind,action.value)).fetchone()["status"]
                    elif action.kind == "hr":
                        result["case_id"] = conn.execute("SELECT korsik.open_hr_case(%s,%s,%s,%s) AS id",
                            (eid,msg,"explicit_human",payload.get("body", "") or "Запрос связи с HR.")).fetchone()["id"]
                    elif action.kind == "case_followup":
                        case = conn.execute("SELECT id FROM korsik.hr_cases WHERE id=%s AND employee_id=%s "
                            "AND status='awaiting_hr' FOR UPDATE", (action.case_id,eid)).fetchone()
                        if not case:
                            raise ValueError("Open HR context no longer current; show current thread")
                    elif action.kind == "rag_question":
                        # Durable pending work. A separate RAG worker claims and computes it.
                        conn.execute("UPDATE korsik.messages SET rag_status='pending' WHERE id=%s", (msg,))
                    elif action.kind == "plan_question":
                        plan = conn.execute("SELECT count(s.id) AS total, "
                            "count(s.id) FILTER (WHERE s.status='completed') AS completed "
                            "FROM korsik.employee_plans p JOIN korsik.plan_events s ON s.plan_id=p.id "
                            "WHERE p.employee_id=%s AND p.status='active' GROUP BY p.id", (eid,)).fetchone()
                        answer = "Сейчас у вас нет активного плана адаптации." if not plan else (
                            f"В вашем плане выполнено {plan['completed']} из {plan['total']} этапов.")
                        conn.execute("SELECT korsik.queue_message(%s,'bot',%s,%s,'notice',NULL,NULL,NULL,%s)",
                                     (eid,answer,f"plan-answer:{msg}",msg))
                    elif action.kind == "smalltalk":
                        conn.execute("SELECT korsik.queue_message(%s,'bot',%s,%s,'notice',NULL,NULL,NULL,%s)",
                                     (eid,"Я здесь, чтобы помочь с адаптацией. Чем могу помочь?",f"smalltalk:{msg}",msg))
                    elif action.kind in {"stale_action","invalid_answer"}:
                        hint = "Этот вопрос уже завершён или изменился. Откройте актуальное сообщение." if action.kind == "stale_action" else "Ответ не подходит к этому вопросу. Проверьте формат и попробуйте ещё раз."
                        conn.execute("SELECT korsik.queue_message(%s,'bot',%s,%s,'notice',NULL,NULL,NULL,%s)",
                                     (eid,hint,f"input-hint:{msg}",msg))
                    elif action.kind == "assistant_identity":
                        conn.execute("SELECT korsik.queue_message(%s,'bot',%s,%s,'notice',NULL,NULL,NULL,%s)",
                            (eid,"Я — автоматизированный помощник Корсик. При необходимости передам ваш вопрос закреплённому HR.",f"identity:{msg}",msg))
                    else:
                        raise ValueError("Unsupported route")
                    conn.execute("UPDATE korsik.max_inbox SET employee_id=%s WHERE event_id=%s", (eid,inbox["event_id"]))
            conn.execute("UPDATE korsik.max_inbox SET status='done',lease_until=NULL,result=%s,last_error=NULL "
                         "WHERE event_id=%s", (Jsonb(result),inbox["event_id"]))
        return result
    except Exception:
        # Failed business changes were rolled back; leave a durable retry or DLQ.
        # Store only a sanitized error code; never dump a token or PII in last_error.
        with conn.transaction():
            conn.execute("""UPDATE korsik.max_inbox SET
              status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,
              lease_until=NULL,next_attempt_at=clock_timestamp()+interval '30 seconds',
              last_error='PROCESSING_FAILED'
              WHERE event_id=%s AND status='processing' AND generation=%s""",
              (claim["event_id"],claim["generation"]))
        raise


class RejectedBeforeAcceptance(Exception):
    """Adapter has explicit evidence that MAX did not accept the message."""


def send_batch(conn, max_send: Callable, limit: int = 10):
    # One claim at a time: do not let a large batch's leases expire while waiting
    # sequentially behind earlier network calls. Production may parallelize.
    for _ in range(limit):
        with conn.transaction():
            rows = conn.execute("SELECT * FROM korsik.claim_outbox(1,true)").fetchall()
        if not rows:
            return
        item = rows[0]
        try:
            # External call AFTER commit; set bounded timeouts in the adapter.
            # If MAX supports idempotency, use str(message_id) as its stable key.
            ext_id = max_send(chat_id=item["max_chat_id"],text=item["body"],
                              operation_key=str(item["message_id"]),payload=item["payload"],
                              employee_id=item["employee_id"],plan_event_id=item["plan_event_id"])
        except RejectedBeforeAcceptance:
            with conn.transaction():
                conn.execute("SELECT korsik.finish_send(%s,%s,NULL,%s,false)",
                             (item["message_id"],item["generation"],"MAX_REJECTED"))
        except Exception:
            with conn.transaction():
                conn.execute("SELECT korsik.finish_send(%s,%s,NULL,%s,true)",
                             (item["message_id"],item["generation"],"MAX_RESULT_UNKNOWN"))
        else:
            with conn.transaction():
                conn.execute("SELECT korsik.finish_send(%s,%s,%s)",
                             (item["message_id"],item["generation"],ext_id))


def hr_employee(conn, authenticated_hr_id: int, employee_id: int):
    """Every HR route must scope by the HR identity from the authenticated session.
    Do not take authenticated_hr_id from a URL, request JSON or user-entered role.
    Same check is required for message history, documents, exports and sources.
    """
    with conn.transaction():
        row = conn.execute("SELECT e.* FROM korsik.employees e JOIN korsik.hr_users h ON h.id=e.hr_id "
                           "WHERE e.id=%s AND e.hr_id=%s AND h.enabled",
                           (employee_id,authenticated_hr_id)).fetchone()
        if row is None:
            raise PermissionError("Not found in this HR's employee list")
        return row
