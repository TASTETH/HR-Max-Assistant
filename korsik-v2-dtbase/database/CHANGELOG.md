# v2 / migration 003

Изменения по спецификации из приложенного текста от 24.10.2026.

| API/объект | Изменение |
|---|---|
| install.sql | Теперь применяет миграции 1, 2, 3; не выполнять поверх v1 |
| migrations/003_survey_router.sql | Upgrade только выпущенной v1; транзакция, advisory lock, preflight, отказ при повторе |
| assign_plan(hr,employee,version[,calendar]) | Четвёртый optional календарь; аудитория, выбранный immutable definition, probation/workday |
| reschedule_plan(hr,plan,date,zone[,probation_end]) | Пятый optional якорь; scheduled-only, календарь snapshot |
| configure_event | Прежние аргументы, новый расчёт send_until/response preview |
| queue_message | Дополнительный десятый optional payload; старые SQL-вызовы допустимы |
| claim_outbox(limit[,global_serial]) | Возвращает 7 колонок, включая payload/employee/plan_event; один in-flight сотруднику после COMMIT |
| answer_event | Прежние 4 аргумента, typed legacy storage прежний; rating/feedback ставят durable анализ; stale comment prompt отменяется |
| answer_survey_question | Новый API с event/question/type/JSON value/revision/source message |
| toggle_survey_option | Draft multi-select, отдельный submit |
| event_answers | Новый составной PK; только для kind=survey |
| message_analysis | Старый composite PK заменён bigint job id + business UNIQUE; старые результаты сохранены legacy_summary |
| pending_interactions | Новая таблица для уточнений оригинала без переписывания inbox |
| hr_cases | Источник ответа, category/priority, one open source, явное closure_basis |
| employees/hr_users | Площадка/тип/ты-вы/конец испытательного срока/context revision; verified HR contact |
| messages/outbox | Bounded immutable payload; durable per-employee mutex через FK/UNIQUE |
| documents | Добавлена nullable scope_location_tag; источники проверяются до LLM и перед send |

Не добавлены параллельные onboarding_* таблицы, второй analysis_jobs, векторное хранилище PostgreSQL, универсальный workflow/RBAC, новая схема на HR или демонстрационные партиции. Новые реальные таблицы только event_answers и pending_interactions.

Требуется обновить backend/SQLAlchemy: analysis PK/upsert, claim return shape, payload renderer, callback target/revision и two-phase routing. В примере sender временно global_serial=true / concurrency=1. Полная совместимость действующего app/hr/ не объявлена без его исходников и запуска.

Миграция не zero-downtime и не универсальный перенос старых данных. Rehearsal/backup/native tests обязательны перед корпоративным запуском. Обратная миграция, теряющая новые ответы, не поставляется.
