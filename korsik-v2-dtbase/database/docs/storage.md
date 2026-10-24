# Ключи, JSONB, индексы и партиции v2

Полные определения по каждой таблице/индексу — data_dictionary.md, полученный из каталога PostgreSQL после установки. Схема 22 таблицы, одна общая для HR; мультиарендность/RBAC-платформа не добавлены.

## Существенные решения

- Решение → PK event_answers(event_id,question_code) → ответ определяется событием и вопросом → девять ответов одного опроса не затирают друг друга → дополнительный id не нужен; существование code/type в snapshot проверяет trigger, потому что CHECK не должен читать другую строку.
- Решение → FK event_answers.event_id и проверка владельца source_message_id → ответ не относится к чужой форме/чату → answer_survey_question сравнивает employee/event/question/revision → backend всё ещё обязан аутентифицировать MAX; передача ID не разрешение доступа.
- Решение → частичный UNIQUE plan_events(employee_id) для всех интерактивных queued/awaiting состояний → только одна независимая форма → FAQ и info не занимают этот слот → вторая форма ждёт освобождения/своего send_until и может быть пропущена, если просрочена.
- Решение → outbox.employee_id + составной FK(message_id,employee_id) и частичный UNIQUE(employee_id) WHERE sending/uncertain → durable delivery mutex после COMMIT → второй worker не заберёт второе сообщение сотруднику, пока неизвестен исход первого → дополнительная колонка/индекс и осознанное ожидание при uncertain. Это реальное исправление v1, а не только disjoint message_id test.
- Решение → message_analysis PK id BIGINT IDENTITY, UNIQUE(source_key,input_revision,model_version,policy_version) → задание имеет собственные lease/result и устойчивую идентичность входа → комментарий и повторный анализ не теряют историю → старый клиент ON CONFLICT(message_id,model_version) необходимо обновить.
- Решение → source_key на case и UNIQUE для незакрытого источника → комментарии/модели не порождают case на каждый сигнал → одна открытая проблема конкретного ответа → закрытое обращение не переоткрывается автоматически только от переанализа.
- Решение → pending_interactions PK id, FK(original_message_id,employee_id), один pending/один resolved оригиналу → уточнение связано с исходным текстом и выбирается один раз → поздний callback не переписывает inbox.result → после смены контекста нужен новый snapshot; разрешение и бизнес-handler обязаны commit вместе.
- Решение → прежние BIGINT внутренних сущностей и глобальный text PK inbox сохранены → нет второго HR/onboarding набора → тот же webhook через месяц остаётся дублем → нельзя удалять tombstone event_id вместе с транспортным payload.

Все FK ON DELETE RESTRICT / ON UPDATE NO ACTION. История не удаляется каскадом. Идентичности immutable; приложение без DELETE/TRUNCATE. PK и UNIQUE имеют обеспечивающие индексы; отдельные дубли поверх них не создаются. FK не создаёт автоматически индекс на дочерних колонках. Новые рабочие индексы обслуживают pending analysis, leases, текущие clarification, сроки отправки и низкие оценки по вопросу, а не каждое поле.

Цена индекса — место, WAL и работа при записи; частичные индексы меняются со статусом. Новый outbox.employee_id не заменяет message PK, а нужен для строгой уникальности конкурентной доставки. Курсор чата остаётся (created_at,id), без больших OFFSET.

## Ограниченные JSONB

| Поле | Переменная структура | Проверка / индекс / чего не хранить |
|---|---|---|
| template_events.definition | Вопросы четырёх/нового survey типа, опции, аудитория, варианты | Закрытый validator, до 64 KiB/50 вопросов/20 вариантов. Не кадровая карточка и не workflow. Нет GIN |
| plan_events.definition | Один выбранный snapshot | Те же проверки, variants запрещён; snapshot immutable. Не редактировать для персонального исправления ответов |
| employee_plans.work_calendar | Утверждённые даты графика | Только weekdays/holidays/extra_workdays, ISO dates, без дублей/пересечений, до 16 KiB. Immutable. Не универсальная настройка HR |
| event_answers.value | Тип значения зависит от answer_type | CHECK и trigger проверяют boolean/string/array/attachment, выбранные IDs, exclusivity. Рейтинг отдельно SMALLINT. Не байты фото |
| messages.payload | Кнопки, вложения, роль, target/revision | Закрытый validator до 64 KiB, 30 options/10 attachments, immutable; не секреты и не публичные бессрочные URLs |
| pending_interactions.choices/resolution | Ограниченные допустимые routes/цели | 2..10, 8 KiB; операции/trigger проверяют targets/owner/revision. Не произвольные ID модели |
| message_analysis.request/result | Точный HRInput / полный native HRResult разных исходов | Object до 64/128 KiB; типизированные status/source/versions/lease/priority-category вынесены. Не копия вектора/всего корпоративного документа |
| max_inbox.payload/result | Нормализованные разные события / результат обработки | Object 64 KiB для payload; backend allowlist; никаких секретных инвайтов. Не единственное хранилище бизнес-ответов |
| audit_log.details | Малые детали значимых действий | Object 8 KiB, allowlist backend, без текста анкет/секретов/stack trace |

JSONB не индексируется GIN без заданного запроса. Поиск работает по обычным колонкам/PK/status/time; scores — нормализованные поля, а не парсинг payload дашбордом. Полный бизнес-ответ переживает очистку inbox.

## Партиционирование

Остаётся решение v1: обычные таблицы. Допущения, не факты компании: 10 тыс. сообщений/сутки × 24 месяца ≈7,3 млн messages; аудит 500/сутки ≈365 тыс. строк. Реальные размеры/длины текста пока не измерены. Messages имеет много FK и устойчивый курсор; inbox требует глобального event_id; analysis/answers/планы малы относительно чата и имеют связанные жизненные циклы. Для них месячное разбиение сейчас добавляет сложность без доказанной пользы.

Решение → не создавать партиции → простые глобальные PK/FK/UNIQUE → повтор webhook с новым received_at отклоняется → нет дешёвого DROP месяца; архивирование должно учитывать связи. Новые таблицы не меняют это решение.

partition_maintenance.sql read-only показывает размеры/autovacuum. EXPLAIN с UTC-диапазоном и реальный наблюдённый план находятся в queries.sql / tests/explain_observed.txt. Pruning отсутствует, поскольку таблица не partitioned; Index Scan не выдаётся за partition pruning. Времена WASM не являются benchmark сервера.

Если измерения оправдают RANGE по месяцу, первым кандидатом станет audit_log(occurred_at), без входящих FK. PK должен включать occurred_at; для messages потребуется пересмотр всех составных FK/потребителей. UNIQUE(event_id,created_at) не защищает от event_id с новым временем. Inbox нельзя делить так без отдельной глобальной гарантии.

Будущий partition runbook — отдельная проверенная миграция: UTC [начало месяца, начало следующего), заранее текущий+два будущих периода, transaction advisory lock, точная проверка каталога/границ при повторном запуске, мониторинг числа частей и отсутствующего месяца. DEFAULT, если выбран, требует контроля/переноса строк перед ATTACH. Retention и архив/restore/FK проверяются до удаления; никакого бесконтрольного DROP в этом комплекте нет.

Официальные документы PostgreSQL 17: [constraints](https://www.postgresql.org/docs/17/ddl-constraints.html), [partial indexes](https://www.postgresql.org/docs/17/indexes-partial.html), [locking](https://www.postgresql.org/docs/17/explicit-locking.html), [JSON functions](https://www.postgresql.org/docs/17/functions-json.html), [partition limitations](https://www.postgresql.org/docs/17/ddl-partitioning.html#DDL-PARTITIONING-DECLARATIVE-LIMITATIONS), [DST](https://www.postgresql.org/docs/17/datetime-invalid-input.html).
