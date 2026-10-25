"""Employee-initiated delivery of their own adaptation plan; no LLM or corporate RAG."""
from __future__ import annotations

import re

from .analysis import rule_signals
from .cases import open_case
from .outbound import personalize, queue_to_employee

PLAN = re.compile(r"\b(?:план\w*\s+адаптац\w*|адаптационн\w*\s+план\w*|персональн\w*\s+план\w*|личн\w*\s+план\w*)\b", re.I)
ACTION = re.compile(r"\b(?:пришл\w*|отправ\w*|покаж\w*|получ\w*|най\w*|где|посмотр\w*|дай|выдай|скин\w*|скач\w*|предостав\w*)\b", re.I)
SELF = re.compile(r"\b(?:мой|моя|мою|моего|моим|мои|мне|меня|свой|своего|персональн\w*|личн\w*)\b", re.I)
OTHER = re.compile(r"\b(?:чуж\w*|коллег\w*|сотрудник\w*|сотрудниц\w*)\b", re.I)


def is_plan_request(text: str) -> bool:
    text = (text or "").strip().lower().replace("ё", "е")
    if text in ("/plan", "/myplan"):
        return True
    if not PLAN.search(text) or not ACTION.search(text):
        return False
    if SELF.search(text):
        return True
    # Recognise a plain file request without an owner, but not arbitrary task questions.
    rest = PLAN.sub(" ", text)
    rest = ACTION.sub(" ", rest)
    rest = re.sub(r"\b(?:пожалуйста|могу|можно|ли|я|как|файл|еще|раз|мне|для|меня)\b", " ", rest)
    return not re.sub(r"[\s?!.,:;—-]", "", rest)


def handle_plan_request(conn, emp: dict, text: str, mid: int, request_id: str) -> bool:
    if emp.get("dialog_mode") == "hr_active":
        return False
    if not is_plan_request(text) or rule_signals(text):
        return False
    if OTHER.search(text):
        reply = "Я могу прислать только твой персональный план адаптации."
        reply_vy = "Я могу прислать только ваш персональный план адаптации."
        queue_to_employee(conn, emp, personalize(emp, reply, reply_vy),
                          author_kind="bot_system", source="system", request_id=request_id,
                          payload={"reply_to": mid, "kind": "personal_plan_access"})
        return True
    # Deliberately no fallback to a common plan or another employee's materials.
    material = conn.execute("""SELECT * FROM materials WHERE key='adaptation_plan'
                              AND employee_id=%s AND active ORDER BY id DESC LIMIT 1""",
                            (emp["id"],)).fetchone()
    if material is not None and material.get("employee_id") != emp["id"]:
        raise ValueError("personal_plan_owner_mismatch")
    if not material:
        case = open_case(conn, emp, reason="material_missing", priority="low", topic="tasks",
                         question="Сотрудник запросил персональный план адаптации, но он не прикреплён.",
                         root_message_id=mid, dedupe_key=f"personal_plan:{mid}",
                         context={"material_key": "adaptation_plan"}, set_waiting=False)
        queue_to_employee(conn, emp, "План пока не прикреплён. Уточню у коллег из HR-службы и вернусь с ним сюда.",
                          author_kind="bot_system", source="system", request_id=request_id, case_id=case["id"],
                          payload={"reply_to": mid, "kind": "personal_plan_missing"})
        return True
    reply = personalize(emp, "Вот твой персональный план адаптации.", "Вот ваш персональный план адаптации.")
    material_ids = []
    if material["kind"] == "link":
        reply += "\n" + material["url"]
    elif material["kind"] == "text":
        reply += "\n\n" + (material.get("body") or "")
    else:
        material_ids = [material["id"]]
    queue_to_employee(conn, emp, reply, author_kind="bot_system", source="system", material_ids=material_ids,
                      request_id=request_id, payload={"reply_to": mid, "kind": "personal_plan",
                                                      "material_id": material["id"]})
    return True
