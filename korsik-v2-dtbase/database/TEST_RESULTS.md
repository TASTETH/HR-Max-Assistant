# Фактическая проверка редакции 2

Подготовлено 24 октября 2026. Базовая версия — PostgreSQL 17. SQL реально исполнялся на **PostgreSQL 17.5 / PGlite 0.3.14 (WASM)**. Это движок PostgreSQL с DDL, PL/pgSQL, FK/CHECK/UNIQUE, транзакциями и EXPLAIN, не SQLite и не SQL-линтер. Полный version() записан в tests/smoke_observed.log.

## Выполнено

| Проверка | Результат |
|---|---|
| Первая установка schema.sql + operations.sql + migration 003 | PASS на пустом PostgreSQL engine |
| seed_demo.sql | PASS, legacy rating 7+comment/9 и старые данные продолжают работать |
| tests/invariants.sql | PASS: FK/range/replay/owner/active plan/status/history/retry/lease/calendar/duplicate index assertions |
| tests/flows.sql | PASS: отдельный RAG при ожидании комментария, no_answer → HR, technical error, источники, ownership |
| tests/delivery_serial.sql | PASS: одна запись сотруднику на batch, запрет второй после **COMMIT**, блокировка uncertain, глобальный режим после COMMIT |
| seed_survey_demo.sql | PASS: поля площадки/типа, immutable выбранный variant, 9-question survey, 12-question final, workday/probation anchors |
| tests/survey_router.sql | PASS: типы, комментарии ниже 8/при false/other, multiselect draft/submit/exclusive/canonical order, фото-ID, старые revisions, запрет незавершённого completion, 9 accepted ответов, stale prompt suppression, completion retention |
| Те же новые тесты | PASS: send window отличается от response window; задержанная отправка получает полное окно после API acknowledgement; рабочие даты/конец испытательного срока |
| Те же новые тесты | PASS: durable HR analysis, expired generation, superseded старый score после комментария, низкая оценка без автоэскалации, no_data+TicketDraft → case, mixed gate, одно разрешение clarification без перезаписи inbox, закрытие по API acceptance без фальшивого receipt, expiry формы освобождает слот |
| Upgrade из выпущенной v1 с демоданными | PASS: ratings/comments/statuses/scheduled_at/deadline_at/invitations не изменены; старая summary analysis сохранена |
| Upgrade preflight | PASS: несколько активных форм и несколько in-flight sends сотруднику отклонены, без частичной схемы |
| Повтор migration 003 | PASS: отказ по версии, без повторного изменения |
| queries.sql / partition_maintenance.sql | PASS; партиций по-прежнему нет |
| walkthrough.sql / walkthrough_survey.sql | PASS: сохраняемый девятивопросный сценарий, legacy HR delivery; никакой отправки в MAX |
| Object permissions | PASS в WASM через SET ROLE app: необходимые операции доступны; DELETE employees / UPDATE audit / SELECT migrations / CREATE TABLE запрещены |
| Python | py_compile успешен для backend/router examples, concurrency и launcher; это не интеграционный запуск Python-сервиса |
| Словарь | Выгружен из исполненной v2: 22 таблицы, одно view; все поля/constraints/indexes реальные |

Журналы: tests/smoke_observed.log, tests/upgrade_observed.log, tests/permissions_partial_observed.log. Наблюдённый план переписки — tests/explain_observed.txt. Времена WASM не используются как benchmark. Межмесячная уникальность inbox проверена без фиктивного partition pruning.

## Не объявляется проверенным

1. Native PostgreSQL с двумя независимыми соединениями. tests/concurrency.py обновлён: проверяет не только разные message_id, но и разные employee_id после COMMIT и при uncertain. **В этой сессии не запускался**, Docker/psql не были доступны как запущенная native-среда.
2. Полная цепочка DATABASE GRANT → migrator LOGIN → owner → app LOGIN. В PGlite GRANT CONNECT ON DATABASE template1 возвращает embedded-каталожное `XX000 tuple concurrently deleted`. В partial smoke эти DATABASE grants пропущены, объекты создавались embedded-владельцем. Штатные permissions.sql/grants.sql не заменены выдачей SUPERUSER приложению; их необходимо проверить Docker-runbook.
3. Реальный MAX SDK/webhook/HMAC/receipt semantics, лимиты callback, файловый backend и внешняя reconciliation uncertain. Демонстрационные acknowledgement/asset IDs — явно симуляции.
4. Действующий app/hr/process_hr_input(): исходников нет, поэтому фактическая совместимость HRResult/HRInput не доказана. Поставлен контракт нормализующего адаптера; full_result должен содержать полный настоящий объект сервиса, без выдуманных метрик/версий.
5. Рабочий семантический Router, модели, Qdrant/reranker и качество эскалации. DB state/contracts реализованы; внешние компоненты не объявлены работающими.
6. Полная утверждённая транскрипция Excel, реальные картинки, календарь компании и разрешение B53:B56/F54. Демо из 9/12 вопросов проверяет структуру, не заменяет проверку содержания.
7. Перенос произвольной командной БД/старого onboarding_* содержимого. Upgrade протестирован только на выпущенном v1 fixture. До корпоративного применения — реальный DDL/data profile, mapping, backup и rehearsal.
8. Native нагрузка, WAL/PITR, backup/restore, авторизация API двух HR и иные роли руководителей.

## Воспроизведение SQL smoke

```bash
cd database/tests
npm install
npm run smoke
npm run smoke:upgrade
npm run smoke:permissions
```

Основной native путь — docs/operations.md: Docker PostgreSQL 17, отдельная test DB, SQL-проверки и `python tests/run_native.py --allow-write`. Нельзя считать успешный WASM smoke полной production-приёмкой.
