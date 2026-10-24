"""Transactional handlers and effects for the supplied korsik schema."""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid

import psycopg
from psycopg.types.json import Jsonb

from app.hr.schemas import HRInput
from .schemas import (Action, Attachment, Button, Context, Decision, IncomingEvent,
                      InvalidAction, QuestionSettings, RetryLater, Route, RouterResult)


class Effects:
    def __init__(self, conn, employee_id: int, key: str):
        self.conn, self.employee_id, self.key = conn, employee_id, key
        self.outputs: list[int] = []
        self.jobs: list[int] = []
        self._counter = 0

    def button(self, label: str, route: Route, **kwargs) -> Button:
        token = "r:" + secrets.token_urlsafe(18)
        action = Action(route=route, **kwargs)
        self.conn.execute("INSERT INTO korsik_router.actions(token_hash,employee_id,action) VALUES(%s,%s,%s)",
            (hashlib.sha256(token.encode()).digest(), self.employee_id, Jsonb(action.model_dump(mode="json"))))
        return Button(label=label, callback_data=token)

    def queue(self, text: str, *, buttons=None, attachments=None, key=None, purpose="notice", event_id=None, reply_to=None):
        self._counter += 1
        row = self.conn.execute("SELECT korsik.queue_message(%s,'bot',%s,%s,%s,%s,NULL,NULL,%s) AS id",
            (self.employee_id, text, key or f"router:{self.key}:{self._counter}", purpose, event_id, reply_to)).fetchone()
        mid = row["id"]
        data = {"buttons": [b.model_dump(mode="json") for b in buttons or []],
                "attachments": [a.model_dump(mode="json") for a in attachments or []]}
        self.conn.execute("""INSERT INTO korsik_router.envelopes(message_id,data) VALUES(%s,%s)
            ON CONFLICT(message_id) DO UPDATE SET data=excluded.data""", (mid, Jsonb(data)))
        self.outputs.append(mid)
        return mid

    def set_input(self, mode: dict):
        self.conn.execute("""INSERT INTO korsik_router.profiles(employee_id,input_mode,revision) VALUES(%s,%s,1)
            ON CONFLICT(employee_id) DO UPDATE SET input_mode=excluded.input_mode,revision=korsik_router.profiles.revision+1""",
            (self.employee_id, Jsonb(mode)))

    def clear_question_input(self, event_id: int):
        self.conn.execute("""UPDATE korsik_router.profiles SET input_mode='{}',revision=revision+1
            WHERE employee_id=%s AND input_mode->>'target_id'=%s""", (self.employee_id, str(event_id)))

    def hr_job(self, message_id: int, root_id: int, data: HRInput, *, rag=False):
        row = self.conn.execute("""INSERT INTO korsik_router.hr_jobs
            (deduplication_key,employee_id,message_id,root_message_id,input,continue_with_rag)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(deduplication_key) DO NOTHING RETURNING id""",
            (f"message:{message_id}", self.employee_id, message_id, root_id, Jsonb(data.model_dump(mode="json")), rag)).fetchone()
        if row:
            self.jobs.append(row["id"])

    def open_case(self, root_id: int, reason: str, summary: str, priority="low", category="general_hr_request", source_id=None):
        source_id = source_id or root_id
        previous = self.conn.execute("SELECT id,status FROM korsik.hr_cases WHERE root_message_id=%s", (root_id,)).fetchone()
        if previous and previous["status"] != "awaiting_hr" and source_id != root_id:
            root_id = source_id
        try:
            cid = self.conn.execute("SELECT korsik.open_hr_case(%s,%s,%s,%s) AS id",
                (self.employee_id, root_id, reason, summary)).fetchone()["id"]
        except psycopg.errors.RaiseException as exc:
            if "in flight" in str(exc) or "uncertain" in str(exc):
                raise RetryLater("HANDOFF_WAITING_FOR_SEND") from exc
            raise
        self.conn.execute("""INSERT INTO korsik_router.case_details(case_id,priority,category,source_message_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT(case_id) DO UPDATE SET
            priority=CASE WHEN array_position(ARRAY['low','medium','high'],excluded.priority)>
              array_position(ARRAY['low','medium','high'],korsik_router.case_details.priority)
              THEN excluded.priority ELSE korsik_router.case_details.priority END,
            category=CASE WHEN excluded.category<>'general_hr_request' AND
              array_position(ARRAY['low','medium','high'],excluded.priority)>=
              array_position(ARRAY['low','medium','high'],korsik_router.case_details.priority)
              THEN excluded.category ELSE korsik_router.case_details.category END,
            source_message_id=CASE WHEN array_position(ARRAY['low','medium','high'],excluded.priority)>=
              array_position(ARRAY['low','medium','high'],korsik_router.case_details.priority)
              THEN excluded.source_message_id ELSE korsik_router.case_details.source_message_id END""", (cid, priority, category, source_id))
        self.conn.execute("""UPDATE korsik.messages SET case_id=%s
            WHERE id=%s AND employee_id=%s AND (case_id IS NULL OR case_id=%s)""", (cid, source_id, self.employee_id, cid))
        notice = self.conn.execute("SELECT id FROM korsik.messages WHERE deduplication_key=%s", (f"case:{cid}:notice",)).fetchone()
        if notice:
            buttons = [self.button("Добавить подробности", Route.HR_FOLLOWUP, target_id=cid)]
            self.conn.execute("""INSERT INTO korsik_router.envelopes(message_id,data) VALUES(%s,%s)
                ON CONFLICT(message_id) DO UPDATE SET data=excluded.data""",
                (notice["id"], Jsonb({"buttons": [b.model_dump(mode="json") for b in buttons]})))
            self.outputs.append(notice["id"])
        return cid

    def question_row(self, event_id: int):
        row = self.conn.execute("""SELECT e.*,c.config,a.revision,a.stage,a.selected,a.score,a.comment answer_comment,a.root_message_id
            FROM korsik.plan_events e JOIN korsik.employee_plans p ON p.id=e.plan_id
            LEFT JOIN korsik_router.question_configs c ON c.event_id=e.id
            LEFT JOIN korsik_router.answers a ON a.event_id=e.id
            WHERE e.id=%s AND e.employee_id=%s AND p.status='active' FOR UPDATE OF e""", (event_id, self.employee_id)).fetchone()
        if not row:
            raise InvalidAction("Этот вопрос больше не доступен.")
        row["settings"] = None if row["kind"] == "info" else QuestionSettings.model_validate(row["config"] or
            {"kind": row["kind"], "survey_id": str(row["plan_id"]), "prompt": row["body"]})
        row["revision"] = row["revision"] or 0
        row["selected"] = row["selected"] or []
        return row

    def present(self, event_id: int):
        info = self.conn.execute("SELECT * FROM korsik.plan_events WHERE id=%s AND employee_id=%s AND kind='info' FOR UPDATE",
                                 (event_id, self.employee_id)).fetchone()
        if info:
            if info["status"] == "scheduled":
                self.queue(info["body"], key=f"event:{event_id}", purpose="event", event_id=event_id)
                self.conn.execute("UPDATE korsik.plan_events SET status='queued' WHERE id=%s", (event_id,))
            return
        row = self.question_row(event_id)
        if row["status"] not in {"scheduled", "queued", "awaiting_answer", "awaiting_comment"}:
            raise InvalidAction("Ответ на этот вопрос уже принят.")
        now = self.conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if row["deadline_at"] <= now or row["scheduled_at"] > now:
            raise InvalidAction("Этот вопрос сейчас не принимает ответы.")
        if row["kind"] == "info":
            if row["status"] == "scheduled":
                self.queue(row["body"], key=f"event:{event_id}", purpose="event", event_id=event_id)
                self.conn.execute("UPDATE korsik.plan_events SET status='queued' WHERE id=%s", (event_id,))
            return
        settings = row["settings"]
        rev = row["revision"]
        comment = row["stage"] == "awaiting_comment" or row["status"] == "awaiting_comment"
        buttons = []
        text = settings.prompt
        if comment:
            text = "Расскажите, пожалуйста, подробнее: " + settings.prompt
        elif settings.kind == "rating":
            buttons = [self.button(str(n), Route.SURVEY_ANSWER, target_id=event_id, revision=rev, value=str(n)) for n in range(1, 11)]
        elif settings.kind == "task":
            buttons = [self.button("Готово", Route.SURVEY_ANSWER, target_id=event_id, revision=rev, value="yes")]
        elif settings.kind in {"choice", "multiselect"}:
            route = Route.SURVEY_TOGGLE if settings.kind == "multiselect" else Route.SURVEY_ANSWER
            buttons = [self.button(("✓ " if o.code in row["selected"] else "") + o.label,
                route, target_id=event_id, revision=rev, value=o.code) for o in settings.options]
            if settings.kind == "multiselect":
                buttons.append(self.button("Подтвердить выбор", Route.SURVEY_SUBMIT, target_id=event_id, revision=rev))
        buttons.append(self.button("Связаться с HR", Route.HR_MENU))
        if row["status"] == "scheduled":
            self.queue(text, buttons=buttons, key=f"event:{event_id}", purpose="event", event_id=event_id)
            self.conn.execute("UPDATE korsik.plan_events SET status='queued' WHERE id=%s", (event_id,))
        else:
            self.queue(text, buttons=buttons, event_id=event_id)
        if comment or settings.kind == "feedback":
            self.set_input({"kind": "comment" if comment else "survey", "target_id": event_id, "revision": rev})

    def advance(self, row, selected: list[str]):
        settings = row["settings"]
        target = next((o.next_event_id for o in settings.options if o.code in selected and o.next_event_id), None) or settings.next_event_id
        if target:
            candidate = self.conn.execute("""SELECT id,position,status,scheduled_at FROM korsik.plan_events
                WHERE id=%s AND plan_id=%s AND employee_id=%s""", (target, row["plan_id"], self.employee_id)).fetchone()
            if not candidate or candidate["position"] <= row["position"]:
                raise InvalidAction("Неверная конфигурация следующего вопроса.")
            self.conn.execute("""UPDATE korsik.plan_events e SET status='skipped'
                FROM korsik_router.question_configs c WHERE c.event_id=e.id AND e.plan_id=%s
                  AND e.position>%s AND e.position<%s AND e.status='scheduled'
                  AND c.config->>'survey_id'=%s""", (row["plan_id"], row["position"], candidate["position"], settings.survey_id))
        else:
            candidate = self.conn.execute("""SELECT e.id,e.status,e.scheduled_at FROM korsik.plan_events e
                JOIN korsik_router.question_configs c ON c.event_id=e.id
                WHERE e.plan_id=%s AND e.position>%s AND e.status='scheduled'
                  AND c.config->>'survey_id'=%s ORDER BY e.position LIMIT 1""",
                (row["plan_id"], row["position"], settings.survey_id)).fetchone()
        if candidate and candidate["status"] == "scheduled":
            now = self.conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if candidate["scheduled_at"] <= now:
                self.present(candidate["id"])


class Handlers(Effects):
    def __init__(self, conn, event: IncomingEvent, context: Context, decision: Decision):
        super().__init__(conn, context.employee_id, event.key)
        self.event, self.context, self.decision = event, context, decision
        self.message_id = None
        self.case_id = None

    def incoming(self):
        if self.message_id:
            return self.message_id
        body = self.event.text or ("[Вложение]" if self.event.attachments else "[Действие пользователя]")
        if self.event.kind == "callback":
            question = self.context.question(self.decision.target_id)
            if question and self.decision.route == Route.SURVEY_ANSWER:
                option = next((o for o in question.settings.options if o.code == self.decision.value), None)
                body = option.label if option else self.decision.value or body
            elif question and self.decision.route == Route.SURVEY_SUBMIT:
                body = "\n".join(o.label for o in question.settings.options if o.code in question.selected) or body
        target = self.decision.target_id if self.decision.route in {
            Route.SURVEY_SHOW, Route.SURVEY_ANSWER, Route.SURVEY_COMMENT, Route.SURVEY_TOGGLE, Route.SURVEY_SUBMIT} else None
        self.message_id = self.conn.execute("""INSERT INTO korsik.messages
            (employee_id,direction,author,body,inbox_event_id,event_id)
            VALUES(%s,'in','employee',%s,%s,%s) RETURNING id""",
            (self.employee_id, body, self.event.key, target)).fetchone()["id"]
        self.conn.execute("INSERT INTO korsik_router.envelopes(message_id,data) VALUES(%s,%s)",
            (self.message_id, Jsonb({"kind": self.event.kind, "attachments": [a.model_dump(mode="json") for a in self.event.attachments]})))
        return self.message_id

    def result(self, *, route=None, reason=None, notice=""):
        from .repository import PostgresRepository
        return RouterResult(event_id=self.event.key, status="done", route=route or self.decision.route,
            reason=reason or self.decision.reason, employee_id=self.employee_id, message_id=self.message_id,
            case_id=self.case_id, outgoing=PostgresRepository.outgoing(self.conn, self.outputs), job_ids=self.jobs, notice=notice)

    def invalid(self, notice: str):
        if self.employee_id:
            self.incoming()
            self.queue(notice)
        return self.result(route=Route.STALE, reason="invalid_input", notice=notice)

    def dispatch(self):
        route = self.decision.route
        if route == Route.DELIVERY:
            try:
                self.conn.execute("SELECT korsik.confirm_delivery(%s,%s)", (self.event.external_message_id, self.event.delivered_at))
            except psycopg.errors.NoDataFound as exc:
                raise RetryLater("DELIVERY_PRECEDES_ACCEPTANCE") from exc
            return self.result()
        if route == Route.ACTIVATE:
            try:
                self.employee_id = self.conn.execute("SELECT korsik.activate_invitation(%s,%s,%s) AS id",
                    (bytes.fromhex(self.event.invitation_hash), self.event.account_id, self.event.chat_id)).fetchone()["id"]
            except (psycopg.errors.RaiseException, psycopg.errors.UniqueViolation) as exc:
                raise InvalidAction("Приглашение недействительно, истекло или уже использовано.") from exc
            self.registration_prompt()
            return self.result()
        if route == Route.ACTIVATE_REQUIRED:
            return self.result(notice="Для подключения откройте персональное приглашение от HR.")
        self.incoming()
        if route == Route.RESOLVE:
            self.resolve()
        else:
            self.execute()
        return self.result()

    def execute(self):
        route = self.decision.route
        if route == Route.HR_MENU:
            self.queue("Выберите способ связи с закреплённым HR.", buttons=[
                self.button("Связаться напрямую", Route.HR_CONTACT), self.button("Передать через Корсика", Route.HR_REQUEST)])
        elif route == Route.HR_CONTACT:
            contact = self.conn.execute("""SELECT c.url FROM korsik_router.hr_contacts c JOIN korsik.employees e ON e.hr_id=c.hr_id
                JOIN korsik.hr_users h ON h.id=c.hr_id WHERE e.id=%s AND h.enabled""", (self.employee_id,)).fetchone()
            self.queue("Контакт закреплённого HR." if contact else "Контакт пока не указан. Можно передать запрос через Корсика.",
                buttons=[Button(label="Открыть диалог", url=contact["url"])] if contact else [self.button("Передать запрос HR", Route.HR_REQUEST)])
        elif route == Route.HR_REQUEST:
            self.case_id = self.open_case(self.message_id, "explicit_human", self.event.text or "Сотрудник запросил связь с HR.")
            self.hr_job(self.message_id, self.message_id, self.hr_input("hr_request"))
        elif route == Route.HR_FOLLOWUP:
            self.followup()
        elif route == Route.REGISTRATION:
            self.registration()
        elif route == Route.PREFERENCE:
            if self.decision.value not in {"formal", "informal"}:
                raise InvalidAction("Выберите «ты» или «вы» с помощью кнопки.")
            self.conn.execute("""INSERT INTO korsik_router.profiles(employee_id,preferred_address,revision) VALUES(%s,%s,1)
                ON CONFLICT(employee_id) DO UPDATE SET preferred_address=excluded.preferred_address,revision=korsik_router.profiles.revision+1""",
                (self.employee_id, self.decision.value))
            self.queue("Хорошо, будем общаться на «ты»." if self.decision.value == "informal" else "Хорошо, будем общаться на «вы».")
        elif route in {Route.SURVEY_ANSWER, Route.SURVEY_COMMENT, Route.SURVEY_TOGGLE, Route.SURVEY_SUBMIT}:
            self.survey()
        elif route == Route.SURVEY_SHOW:
            self.show_survey()
        elif route == Route.PLAN:
            row = self.conn.execute("""SELECT count(e.id) AS total,count(e.id) FILTER(WHERE e.status='completed') AS completed
                FROM korsik.employee_plans p JOIN korsik.plan_events e ON e.plan_id=p.id
                WHERE p.employee_id=%s AND p.status='active' GROUP BY p.id""", (self.employee_id,)).fetchone()
            self.queue(f"В вашем плане выполнено {row['completed']} из {row['total']} этапов." if row else "Сейчас у вас нет активного плана адаптации.")
        elif route == Route.CONTENT:
            self.content()
        elif route == Route.RAG:
            self.conn.execute("UPDATE korsik.messages SET rag_status='pending' WHERE id=%s", (self.message_id,))
        elif route in {Route.FEEDBACK, Route.MIXED}:
            self.hr_job(self.message_id, self.message_id, self.hr_input("message"), rag=route == Route.MIXED)
            self.queue("Спасибо, ваше сообщение сохранено. Можно также обратиться к HR.",
                       buttons=[self.button("Связаться с HR", Route.HR_MENU)])
        elif route == Route.CLARIFY:
            self.clarify()
        elif route == Route.PROFILE:
            self.set_input({})
            self.queue("Описание и вложения сохранены.")
        elif route == Route.IDENTITY:
            self.queue("Я — автоматизированный помощник Корсик. Помогаю с адаптацией, ищу ответы в материалах компании и передаю обращения HR.")
        elif route == Route.GREETING:
            self.queue("Здравствуйте! Чем могу помочь с адаптацией?", buttons=[self.button("Связаться с HR", Route.HR_MENU)])
        elif route == Route.HELP:
            if self.context.lifecycle == "registering":
                self.registration_prompt()
            else:
                self.queue("Можно задать вопрос, посмотреть свой план или связаться с HR.", buttons=[
                    self.button("Мой план", Route.PLAN), self.button("Продолжить опрос", Route.SURVEY_SHOW),
                    self.button("Связаться с HR", Route.HR_MENU), self.button("Общаться на ты", Route.PREFERENCE, value="informal"),
                    self.button("Общаться на вы", Route.PREFERENCE, value="formal")])
        elif route == Route.STALE:
            self.queue("Это действие уже завершено или изменилось. Откройте актуальный вопрос.",
                       buttons=[self.button("Текущий опрос", Route.SURVEY_SHOW)])
        else:
            self.queue("Это действие пока не поддерживается. Используйте /help или /HR.")

    def hr_input(self, source: str, *, text=None, question=None, score=None):
        data = dict(event_id=self.event.key, employee_id=str(self.employee_id), source=source,
                    text=self.event.text if text is None else text, created_at=self.event.occurred_at)
        if source == "survey":
            data.update(survey_id=question["settings"].survey_id, question_id=str(question["id"]),
                        question=question["settings"].prompt, score=score)
        return HRInput.model_validate(data)

    def registration_prompt(self):
        employee = self.conn.execute("SELECT * FROM korsik.employees WHERE id=%s", (self.employee_id,)).fetchone()
        step, revision = employee["registration_step"], employee["registration_revision"]
        if employee["lifecycle"] != "registering":
            self.set_input({})
            self.queue("Анкета ожидает проверки HR." if employee["lifecycle"] == "hr_review" else "Регистрация уже завершена.")
            return
        self.set_input({"kind": "registration", "field": step, "revision": revision})
        prompts = {"full_name": "Укажите, пожалуйста, ваши имя и фамилию.",
                   "phone": "Укажите телефон в международном формате, например +79991234567.",
                   "confirm": "Подтвердите заполненную анкету."}
        buttons = [self.button("Подтвердить", Route.REGISTRATION, revision=revision, value="confirm")] if step == "confirm" else []
        self.queue(prompts[step], buttons=buttons)

    def registration(self):
        if self.context.lifecycle != "registering" or self.decision.revision != self.context.registration_revision:
            raise InvalidAction("Шаг регистрации изменился. Откройте /start.")
        field = self.decision.value
        value = "yes" if field == "confirm" and self.event.kind == "callback" else self.event.text
        if field == "confirm":
            if value.casefold() not in {"yes", "да", "подтверждаю"}:
                raise InvalidAction("Подтвердите анкету с помощью кнопки.")
            value = "yes"
        if field == "phone" and not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
            raise InvalidAction("Введите телефон в формате +79991234567.")
        if field not in {"full_name", "phone", "confirm"}:
            raise InvalidAction("Неизвестный шаг регистрации.")
        self.conn.execute("SELECT korsik.save_registration(%s,%s,%s,%s)",
                          (self.employee_id, self.decision.revision, field, value))
        self.registration_prompt()

    def followup(self):
        cid = self.decision.target_id
        if cid not in self.context.open_cases:
            raise InvalidAction("Это обращение уже обработано. Создайте новое обращение через /HR.")
        if self.event.kind == "callback" and not self.event.text:
            self.set_input({"kind": "hr", "target_id": cid})
            self.queue("Напишите подробности для HR.")
            return
        self.conn.execute("UPDATE korsik.messages SET case_id=%s WHERE id=%s", (cid, self.message_id))
        self.case_id = cid
        self.set_input({})
        self.queue("Сообщение добавлено к вашему обращению.")

    def content(self):
        code = self.decision.value or "food"
        row = self.conn.execute("""SELECT body,attachments FROM korsik_router.materials
            WHERE code=%s AND enabled AND (site_code=%s OR site_code IS NULL)
            ORDER BY (site_code IS NOT NULL) DESC LIMIT 1""", (code, self.context.site_code)).fetchone()
        if not row:
            self.queue("Материал для вашей площадки пока не указан. Можно уточнить у HR.",
                       buttons=[self.button("Связаться с HR", Route.HR_MENU)])
        else:
            self.queue(row["body"], attachments=[Attachment.model_validate(a) for a in row["attachments"]])

    def show_survey(self):
        target = self.decision.target_id
        if target:
            self.present(target)
        elif len(self.context.questions) == 1:
            self.present(self.context.questions[0].id)
        elif self.context.questions:
            self.queue("Выберите вопрос, который хотите продолжить.", buttons=[
                self.button(q.settings.prompt[:100], Route.SURVEY_SHOW, target_id=q.id, revision=q.revision)
                for q in self.context.questions])
        else:
            self.queue("Сейчас нет вопросов, ожидающих ответа.")

    def survey(self):
        row = self.question_row(self.decision.target_id)
        if row["status"] == "queued":
            raise RetryLater("QUESTION_DELIVERY_NOT_COMMITTED")
        now = self.conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if row["status"] not in {"awaiting_answer", "awaiting_comment"} or row["deadline_at"] <= now:
            raise InvalidAction("Этот вопрос больше не принимает ответы.")
        if self.decision.revision is not None and self.decision.revision != row["revision"]:
            raise InvalidAction("Вопрос изменился. Откройте его заново.")
        settings = row["settings"]
        self.conn.execute("INSERT INTO korsik_router.answers(event_id,employee_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (row["id"], self.employee_id))
        value = self.decision.value if self.event.kind == "callback" else self.event.text
        if len(value) > 3000:
            raise InvalidAction("Ответ сохранён в истории. Пришлите комментарий к опросу длиной до 3000 символов.")
        selected = list(row["selected"])
        root = row["root_message_id"] or self.message_id
        comment = self.decision.route == Route.SURVEY_COMMENT
        if comment:
            if row["stage"] != "awaiting_comment" and row["status"] != "awaiting_comment":
                raise InvalidAction("Этот вопрос сейчас не ожидает комментарий.")
            if not value:
                self.set_input({"kind": "comment", "target_id": row["id"], "revision": row["revision"]})
                self.queue("Напишите, пожалуйста, комментарий.", event_id=row["id"])
                return
            final = "\n".join([o.label for o in settings.options if o.code in selected] + [value])
            answer_type = "comment" if settings.kind == "rating" else "feedback"
            self.conn.execute("SELECT korsik.answer_event(%s,%s,%s,%s)", (self.employee_id, row["id"], answer_type, final))
            self.conn.execute("""UPDATE korsik_router.answers SET stage='completed',comment=%s,
                root_message_id=%s,revision=revision+1 WHERE event_id=%s""", (value, root, row["id"]))
            self.hr_job(self.message_id, root, self.hr_input("survey", text=value, question=row, score=row["score"] or row["rating"]))
        elif settings.kind == "rating":
            if not re.fullmatch(r"(?:10|[1-9])", value):
                raise InvalidAction("Выберите целую оценку от 1 до 10.")
            score = int(value)
            stage = self.conn.execute("SELECT korsik.answer_event(%s,%s,'rating',%s) AS stage", (self.employee_id, row["id"], value)).fetchone()["stage"]
            self.conn.execute("UPDATE korsik_router.answers SET score=%s,stage=%s,root_message_id=%s,revision=revision+1 WHERE event_id=%s",
                (score, stage, root, row["id"]))
            self.hr_job(self.message_id, root, self.hr_input("survey", text="", question=row, score=score))
            if score < 8:
                self.set_input({"kind": "comment", "target_id": row["id"], "revision": row["revision"] + 1})
                prompt = self.conn.execute("SELECT id FROM korsik.messages WHERE deduplication_key=%s", (f"comment-prompt:{row['id']}",)).fetchone()
                if prompt:
                    self.outputs.append(prompt["id"])
                return
        elif settings.kind in {"choice", "multiselect"}:
            option = next((o for o in settings.options if value.casefold() in {o.code.casefold(), o.label.casefold()}), None)
            if self.decision.route == Route.SURVEY_TOGGLE:
                if settings.kind != "multiselect" or not option:
                    raise InvalidAction("Выберите один из предложенных вариантов.")
                if option.code in selected:
                    selected.remove(option.code)
                elif option.exclusive:
                    selected = [option.code]
                else:
                    selected = [code for code in selected if not next(o for o in settings.options if o.code == code).exclusive] + [option.code]
                self.conn.execute("UPDATE korsik_router.answers SET selected=%s,revision=revision+1 WHERE event_id=%s", (selected, row["id"]))
                self.present(row["id"])
                return
            if settings.kind == "choice":
                if not option:
                    raise InvalidAction("Выберите один из предложенных вариантов.")
                selected = [option.code]
            elif self.decision.route != Route.SURVEY_SUBMIT or not selected:
                raise InvalidAction("Выберите варианты и нажмите «Подтвердить выбор».")
            labels = "\n".join(o.label for o in settings.options if o.code in selected)
            needs_comment = any(o.comment_required for o in settings.options if o.code in selected)
            stage = "awaiting_comment" if needs_comment else "completed"
            self.conn.execute("UPDATE korsik_router.answers SET selected=%s,stage=%s,root_message_id=%s,revision=revision+1 WHERE event_id=%s",
                (selected, stage, root, row["id"]))
            self.hr_job(self.message_id, root, self.hr_input("survey", text=labels, question=row))
            if needs_comment:
                self.set_input({"kind": "comment", "target_id": row["id"], "revision": row["revision"] + 1})
                self.queue("Расскажите, пожалуйста, подробнее о вашем ответе.", event_id=row["id"])
                return
            self.conn.execute("SELECT korsik.answer_event(%s,%s,'feedback',%s)", (self.employee_id, row["id"], labels))
        elif settings.kind == "task":
            if value.casefold() not in {"yes", "да", "готово"}:
                raise InvalidAction("Для подтверждения задания нажмите «Готово».")
            self.conn.execute("SELECT korsik.answer_event(%s,%s,'task_confirm','yes')", (self.employee_id, row["id"]))
            self.conn.execute("UPDATE korsik_router.answers SET stage='completed',root_message_id=%s,revision=revision+1 WHERE event_id=%s", (root, row["id"]))
        else:
            if not value:
                raise InvalidAction("Напишите текстовый ответ.")
            self.conn.execute("SELECT korsik.answer_event(%s,%s,'feedback',%s)", (self.employee_id, row["id"], value))
            self.conn.execute("UPDATE korsik_router.answers SET stage='completed',comment=%s,root_message_id=%s,revision=revision+1 WHERE event_id=%s", (value, root, row["id"]))
            self.hr_job(self.message_id, root, self.hr_input("survey", text=value, question=row))
        self.clear_question_input(row["id"])
        self.queue("Спасибо, ответ сохранён.")
        self.advance(row, selected)

    def clarify(self):
        targets = []
        if not self.event.attachments:
            targets += [Decision(route=Route.RAG, reason="user_choice"), Decision(route=Route.FEEDBACK, reason="user_choice")]
            targets += [Decision(route=Route.SURVEY_COMMENT if q.stage == "awaiting_comment" else Route.SURVEY_ANSWER,
                reason="user_choice", target_id=q.id, revision=q.revision) for q in self.context.questions[:8]]
        else:
            targets.append(Decision(route=Route.PROFILE, reason="user_choice"))
        targets.append(Decision(route=Route.HR_REQUEST, reason="user_choice"))
        pending_id = str(uuid.uuid4())
        self.conn.execute("INSERT INTO korsik_router.pending(id,employee_id,message_id,targets) VALUES(%s,%s,%s,%s)",
            (pending_id, self.employee_id, self.message_id, Jsonb([t.model_dump(mode="json") for t in targets])))
        labels = {Route.RAG: "Задать вопрос", Route.FEEDBACK: "Сообщить о проблеме", Route.HR_REQUEST: "Передать HR", Route.PROFILE: "Материалы для представления"}
        buttons = []
        for i, target in enumerate(targets):
            q = self.context.question(target.target_id)
            label = labels.get(target.route, "Ответить: " + (q.settings.prompt[:80] if q else "опрос"))
            buttons.append(self.button(label, Route.RESOLVE, pending_id=pending_id, value=str(i)))
        self.queue("К чему относится ваше сообщение? Текст уже сохранён.", buttons=buttons)

    def resolve(self):
        pending = self.conn.execute("""SELECT * FROM korsik_router.pending WHERE id=%s AND employee_id=%s
            AND resolved_at IS NULL AND expires_at>clock_timestamp() FOR UPDATE""",
            (self.decision.pending_id, self.employee_id)).fetchone()
        if not pending or not self.decision.value.isdigit() or int(self.decision.value) >= len(pending["targets"]):
            raise InvalidAction("Это уточнение уже завершено или истекло.")
        selected = Decision.model_validate(pending["targets"][int(self.decision.value)])
        if selected.target_id and selected.route in {Route.SURVEY_ANSWER, Route.SURVEY_COMMENT}:
            question = self.context.question(selected.target_id)
            if not question or question.revision != selected.revision:
                raise InvalidAction("Вопрос изменился. Откройте актуальное уточнение.")
        original = self.conn.execute("""SELECT m.body,m.inbox_event_id,e.data FROM korsik.messages m
            LEFT JOIN korsik_router.envelopes e ON e.message_id=m.id WHERE m.id=%s AND m.employee_id=%s""",
            (pending["message_id"], self.employee_id)).fetchone()
        original_kind = (original["data"] or {}).get("kind", "text")
        self.message_id = pending["message_id"]
        self.event = self.event.model_copy(update={"kind": original_kind, "text": original["body"] if original_kind == "text" else "",
            "attachments": [Attachment.model_validate(a) for a in (original["data"] or {}).get("attachments", [])]})
        self.decision = selected
        self.execute()
        self.conn.execute("UPDATE korsik_router.pending SET resolved_at=clock_timestamp() WHERE id=%s", (pending["id"],))
