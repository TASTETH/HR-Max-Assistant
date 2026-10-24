# Контракты операций v2

Все операции исполняются с SECURITY INVOKER. Backend аутентифицирует MAX/SSO, проверяет HR-область и передаёт trusted IDs. Внутри — короткая транзакция, сначала employee row lock, затем event/question/case/outbox. Сетевые вызовы вне транзакций. `examples/backend.py` показывает inbox/sender и простые обработчики; `examples/router_phases.py` — чтение контекста → внешний классификатор → повторная проверка revision под lock. Это интеграционные примеры, не готовый HTTP Router/SDK MAX.

## Приглашение, регистрация, HR и legacy-события

Сохранились activate_invitation(hash,account,chat), save_registration(employee,revision,field,value), verify_registration(hr,employee,unit,job,date,timezone), answer_event(employee,event,type,value), open_hr_case(employee,message,reason,context), prepare_hr_reply(hr,case,text), confirm_delivery(external_id,time). Их PK/FK/состояния остаются действующими. Оценка 7 ждёт комментарий, 9 завершает простой rating; task_confirm='yes' подтверждает задание.

MAX secret хранится только SHA-256; нормализованный inbox не должен содержать исходный инвайт в тексте/вложенном payload/log. Idempotency — одна business-транзакция + inbox.done/result; duplicate webhook не запускает ответ заново. SQLSTATE пользовательской ошибки должен преобразовываться Router в понятную подсказку и done, а не технический retry. Неожиданная ошибка БД/модели повторяется/попадает в DLQ.

## План и календарь

```python
with conn.transaction():
    plan_id = conn.execute(
        'SELECT korsik.assign_plan(%s,%s,%s,%s)',
        (authenticated_hr, employee_id, published_version, Jsonb(approved_calendar)),
    ).fetchone()[0]
```

assign_plan имеет четвёртый аргумент work_calendar (по умолчанию технические Пн–Пт без праздников). Поля площадки/типа/даты конца испытательного срока берутся из employees. Требуемый probation_end_date должен быть заполнен до назначения. Аудитория top-level исключает неподходящее событие; вариант внутри события заменяет body/questions/attachments/options. При двух совпавших вариантах ошибка, при неизвестной площадке — общий вариант. Если фильтрация удаляет dependency, назначение отклоняется. После назначения variants больше нет: остаётся один выбранный неизменный definition.

- set_employee_context(hr,employee,location,type,style,probation_end) обновляет профиль с проверкой HR. Изменение площадки/типа при active плане требует cancel/replace; календарь и выбранный контент не переписываются.
- set_communication_style(employee,style,context_revision) сохраняет informal/formal. Это отдельная настройка, не sentiment и не должность.
- configure_event(hr,event,response_hours,max_reminders,local_time) работает только для scheduled.
- reschedule_plan(hr,plan,start_date,timezone,probation_end DEFAULT NULL) пересчитывает только scheduled правила тем же календарём. NULL сохраняет прежний конец испытательного срока.
- cancel_plan(hr,plan,true) прекращает адаптацию; cancel_plan(...,false) предназначен для атомарной замены вместе с assign_plan. Сохранённые completion-уведомления не отменяются. Sending/uncertain требуют сверки.

work_calendar: объект ровно с weekdays (ISO 1..7), holidays и extra_workdays (ISO DATE strings). Дополнительный рабочий день имеет приоритет, пересечение holidays/extra запрещено. Снимок неизменен: другой календарь → новый план. workday_no=1 считает первый рабочий день от якоря включительно. При workday_no offsets обязаны быть нулевыми. Обычные offsets применяются календарно; месяц не равен 30 суткам. Сначала локальная дата/время, затем IANA timezone → TIMESTAMPTZ. Preview должен показывать DST-результат.

## Survey API

```python
with conn.transaction():
    conn.execute("SELECT set_config('korsik.analysis_model_version',%s,true)", (deployed_model_version,))
    conn.execute("SELECT set_config('korsik.analysis_policy_version',%s,true)", (deployed_policy_version,))
    state = conn.execute(
        'SELECT korsik.answer_survey_question(%s,%s,%s,%s,%s,%s,%s)',
        (employee_id, event_id, question_code, answer_type,
         Jsonb(answer_value), interaction_revision, incoming_message_id),
    ).fetchone()[0]
```

| type | value | Хранение |
|---|---|---|
| rating | JSON integer 1..10 | rating SMALLINT; value NULL |
| boolean | JSON true/false | value boolean; не score |
| single_choice | JSON string option ID | Проверенный ID; подпись берётся из snapshot |
| multi_choice | JSON array of IDs | После submit канонический порядок опций; без дублей/чужих значений |
| text | JSON string 1..8000 | value string |
| attachment | object asset_ref/kind | Только устойчивый ID проверенного файла |
| comment | JSON nonempty string | comment/commented_at исходного вопроса, новая revision |
| skip | JSON null | Только optional вопрос без ожидаемого комментария; state=skipped |

p_revision — обязательный interaction_revision, а не текущая регистрационная revision или event ID. Старый callback не может примениться к следующему вопросу. p_source_message при наличии обязан быть входящим сообщением того же сотрудника. В реальном inbox он всегда передаётся; SQL-демо может синтезировать запись принятой обратной связи для анализа, не подделывая provider_event_id.

В одном employee может быть одна queued/awaiting_answer/awaiting_comment форма (survey/task/rating/feedback). Это настоящий частичный UNIQUE в plan_events. Info и другие вопросы продолжаются. После overdue/skipped/cancelled/completed слот освобождается; scheduler обрабатывает expiry. Во время sending/uncertain нельзя притворно отменить уже начатую отправку.

Низкий rating требует comment; boolean=false или конкретные choice IDs требуют его только при comment_rule. Собственный вариант — option ID other с обязательным комментарием. Ограниченное when включает вопрос только по принятому ответу более раннего вопроса. optional требует явного skip/ответа. Для выбранной опции можно задать reply_text; он отправляется дословно как завершение вопроса, без LLM.

Для multi-select: toggle_survey_option(employee,event,question,option,revision) меняет draft, повышает revision и создаёт обновлённый prompt. Submit — answer_survey_question с type=multi_choice и окончательным набором. Draft не анализируется. Payload содержит selected_options, answer_type и allow_skip; адаптер отображает toggle/submit/skip по этим полям. Кнопки и источник ответа нельзя угадывать по «последнему вопросу».

## Payload и callback

```json
{
  "role": "prompt",
  "question_code": "contacts",
  "interaction_revision": 4,
  "answer_type": "multi_choice",
  "allow_skip": false,
  "selected_options": ["mentor"],
  "options": [{"id":"mentor","label":"Наставник"},{"id":"hr","label":"HR"}],
  "attachments": []
}
```

Для комментария answer_type=comment. Для completion не требуется активный вопрос; старый prompt после ответа отменяется, completion сохраняется. При info payload может содержать map/pdf/image/file asset_ref. Байты файла, access token и долговечный публичный URL не помещаются в JSON. MAX Adapter превращает утверждённый asset_ref в разрешённый объект/краткоживущую ссылку непосредственно перед отправкой, проверяет существование и владельца входящих вложений.

В примере подпись callback связывает employee + **ID исходящего immutable prompt message** + ID опции/действия. Сервер по этому сообщению получает event/question/revision из payload, проверяет принадлежность/текущее состояние и только затем вызывает SQL-операцию. Вариант ответа также связан подписью. Реальный лимит callback_data и синтаксис MAX проверяет адаптер; HMAC/opaque server-side reference не является обещанием готового SDK MAX.

## Router и уточнение

create_pending_interaction(employee,original_message,context_revision,choices,expires_at) создаёт snapshot выбора и исходящее уточнение. Choices 2..10, закрытые routes: survey/legacy_answer/case_followup/rag_question/hr_request/plan_question. Event/case IDs проверяются на владельца, статус, вопрос, revision, срок. Для targets не используются произвольные JSON workflow/SQL.

resolve_pending_interaction(employee,pending,code,context_revision) блокирует сотрудника и уточнение, повторно проверяет target и сохраняет окончательный выбор. Возвращает original_message_id и trusted choice. **В той же транзакции** backend применяет выбранный обработчик к оригинальному сообщению; ошибка обработчика должна откатить resolution. Inbox нажатия кнопки становится done после этой общей фиксации. Inbox исходного текста остаётся done с прежним result.

Есть UNIQUE на один pending и один resolved для оригинального сообщения. Устаревшее pending отменяется/истекает; можно создать новый snapshot для того же оригинала, если окончательного resolved ещё нет. expire_pending_interactions обслуживает TTL. Изменение context_revision требует пересчёта маршрута/нового уточнения, а не повторного применения старого решения модели.

## HR Intelligence: устойчивые задания

Существующая message_analysis теперь содержит job и результат. enqueue_analysis(employee,message,source_key,input_revision,input,event,question,mixed,model,policy) идемпотентен по source_key/input_revision/model/policy. Простые legacy rating/feedback также ставят анализ через answer_event; источник передаётся транзакционным korsik.source_message_id, иначе SQL-демо создаёт запись принятой обратной связи. Их source_key=`legacy-answer:<event>` и revision события. Task-confirmation не считается содержательной обратной связью. Для нового survey-ответа source_key=`answer:<event>:<question>`, для сообщения `message:<id>`. FK/trigger защищают владельца и происхождение.

Версии берутся из явных параметров либо SET LOCAL custom settings выше. Если они не заданы, работа сохраняется как unconfigured. claim_message_analysis не исполняет unconfigured. После настройки bind_unconfigured_analysis(model,policy,limit) создаёт правильно версионированные задания, помечая старые superseded административным результатом. Для пула настройки задаются транзакционно, а не сессией навсегда.

Worker: claim_message_analysis(model,policy) → COMMIT → реальный process_hr_input() → apply_hr_analysis(job,generation,envelope). Восстановление: recover_analysis_leases(). После 8 ошибок job=dead. Исходный HRInput и полный native HRResult остаются в request/result; не хранится только короткая метка sentiment.

Нормализованный envelope адаптера:

```json
{
  "status":"ok",
  "requires_hr":false,
  "ticket":null,
  "category":"unclear_tasks",
  "priority":"medium",
  "full_result":{"native_result":"полный сериализованный HRResult действующего сервиса"}
}
```

full_result в настоящем коде — **полный объект**, строка native_result выше только поясняет место его размещения. status=ok/no_data/error; ticket=object/null. Дополнительно можно передать topic, sentiment, problem_level, confidence, только если они реально получены и нормализованы. Не выдавать logit за вероятность. Неизвестные scalar-результаты остаются NULL/unknown, не выдумываются.

HRInput.source=survey получает строковый employee_id, event ID как survey_session_id, question_code как question_id, точную формулировку, score только для rating и точные выбранные подписи в стабильном порядке. Для source=hr_request допустим пустой text. Политика категорий/порогов вызывается реальным сервисом, не SQL и не примитивным поиском подстрок. Модель/промпт/политика должны быть версионированы.

Если requires_hr=true **или есть ticket**, создаётся/обновляется case даже при no_data/error. Низкая оценка или negative sentiment сами по себе case не создают. Новая версия комментария не теряет связь с исходным ответом. Поздний результат старой input_revision становится superseded без эскалации. Уже закрытый case не переоткрывается автоматически простым переанализом прежнего сообщения; новая просьба/подробности обрабатываются как новый осмысленный контекст.

Для mixed feedback enqueue_analysis(...,mixed=true) ставит analysis_gate=pending. claim_rag_question/queue_rag_answer/sender не обходят этот барьер. Неэскалирующий успешный анализ ставит clear; эскалация — hr и отмена pending RAG на тот же вопрос. Техническая ошибка оставляет durable retry и доступную явную передачу человеку. При подтверждённом not_found RAG вызывает передачу no_answer; technical error сохраняется отдельно и повторяется.

## Отправка, сроки и неопределённость

claim_outbox(limit,false) выбирает максимум одну запись сотрудника и не выбирает сотрудника с sending/uncertain. Частичный UNIQUE(outbox.employee_id) гарантирует это **после COMMIT**, а составной FK на messages проверяет владельца. claim_outbox возвращает message_id, generation, body, max_chat_id, payload, employee_id, plan_event_id. Старому адаптеру с распаковкой четырёх колонок требуется обновление.

Временный эксплуатационный режим: **один delivery worker**, claim_outbox(1,true). Транзакционный advisory lock сериализует глобальный claim; durable sending/uncertain блокирует следующую отправку вообще. Scheduler допускает несколько worker. Переход к нескольким delivery worker — только после серверных тестов/согласования; SQL уже содержит корректный per-employee mutex.

До первого принятия API scheduled/queued ограничены send_until_at. После принятия интерактивного события deadline_at=accepted_at+response_hours, reminders от того же начала. Пребординг не принимается в очередь после локального начала дня выхода. Если сеть всё-таки приняла устаревшее сообщение после send_until, результат доставки сохраняется, событие становится overdue; база не обещает атомарный отзыв HTTP.

Sender: claim/commit → bounded network call → finish_send(message,generation,external/error). Явный непринятый отказ даёт backoff; timeout/разрыв/истёкший lease — uncertain. Отсутствие receipt не повторяет уже accepted сообщение. После доказанной сверки оператор атомарно фиксирует найденный внешний результат или возвращает pending с новой generation и аудитом; простое «не нашёл при одном запросе» не является доказательством непринятия, пока прежний вызов может быть в полёте. Универсальная автоматическая reconciliation без контракта MAX не реализована.

prepare_hr_reply сохраняет точный текст автора hr и outbox без LLM. close_hr_case(hr,case,'verified_delivery') требует delivered. Альтернатива close_hr_case(hr,case,'api_accepted',nonempty_note) требует sent и явного решения HR, оставляя outbox.accepted и delivered_at=NULL. Разрешение альтернативы в интерфейсе — согласованная бизнес-политика, не автоматический таймер.

assigned_hr_contact(employee) возвращает только verified contact текущего закреплённого enabled HR. Отображение контакта не создаёт case и не обещает прочтение человеком. Явная передача через бота создаёт case без необходимости дождаться модели.
