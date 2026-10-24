# Запуск Docker, upgrade и сопровождение v2

Все команды — для **локальной выделенной PostgreSQL 17**. Compose уже включён в комплект. Docker/psql-клиентская цепочка в этой сессии не запускались: фактическая SQL-проверка выполнена в PostgreSQL 17.5 PGlite/WASM, см. TEST_RESULTS.md.

## Новый локальный стенд

Распакуйте архив и перейдите в database. Нужны Docker Compose v2, OpenSSL и Python для native-тестов. Если старый контейнер уже использует 55432, остановите его без удаления volume или замените внешний порт нового compose.yaml и подключения на 55433. Проект v2 имеет отдельное имя korsik-v2-local, поэтому не переиспользует volume первого стенда автоматически.

```bash
mkdir -p secrets
openssl rand -hex 32 > secrets/postgres_password.txt
chmod 600 secrets/postgres_password.txt
# .gitignore уже исключает секреты, venv, backups и node_modules.
docker compose config --quiet
docker compose pull db
docker compose up -d --wait db
docker compose exec -T db psql -X -U postgres -d korsik_demo -c 'SELECT version();'
```

Данные PostgreSQL 17 находятся в named volume /var/lib/postgresql/data; SQL-каталог примонтирован в /database read-only. На компьютере — 127.0.0.1:55432; в той же Compose-сети — db:5432. POSTGRES_USER=postgres нужен только администрированию локального кластера. Backend использует korsik_app.

```bash
# Роли создаются ОДИН раз на новом кластере.
docker compose exec -T db psql -X -U postgres -d korsik_demo \
  -v ON_ERROR_STOP=1 -v dbname=korsik_demo -f /database/permissions.sql
# Интерактивно: \password korsik_migrator, затем \password korsik_app, затем \q.
docker compose exec db psql -X -U postgres -d korsik_demo
# Теперь настоящая аутентификация мигратора по TCP:
docker compose exec db psql -X -h 127.0.0.1 -U korsik_migrator -d korsik_demo -W \
  -v ON_ERROR_STOP=1 -c 'SET ROLE korsik_owner' \
  -f /database/install.sql -f /database/grants.sql -f /database/seed_demo.sql
# Именно подключение приложения, не административный SET ROLE:
docker compose exec db psql -X -h 127.0.0.1 -U korsik_app -d korsik_demo -W \
  -v ON_ERROR_STOP=1 -f /database/tests/permissions_check.sql
```

Ожидается schema_migrations 1,2,3; владелец таблиц korsik_owner. Роли: owner NOLOGIN; migrator LOGIN с membership owner; app LOGIN без superuser/CREATE/DELETE/TRUNCATE. Доступ HR по-прежнему проверяет backend, RLS нет. Новые event_answers/pending_interactions и рабочие поля analysis доступны доверенной сервисной роли, но финальные ответы/результаты защищены триггерами. Секреты в SQL/repository не включены.

## Отдельная disposable test DB

```bash
docker compose exec -T db createdb -U postgres korsik_test
docker compose exec -T db psql -X -U postgres -d korsik_test -v ON_ERROR_STOP=1 \
  -c 'GRANT CONNECT ON DATABASE korsik_test TO korsik_migrator,korsik_app' \
  -c 'GRANT CREATE ON DATABASE korsik_test TO korsik_owner' \
  -c 'REVOKE CREATE ON SCHEMA public FROM PUBLIC'
docker compose exec db psql -X -h 127.0.0.1 -U korsik_migrator -d korsik_test -W \
  -v ON_ERROR_STOP=1 -c 'SET ROLE korsik_owner' \
  -f /database/install.sql -f /database/grants.sql -f /database/seed_demo.sql \
  -f /database/tests/invariants.sql -f /database/tests/flows.sql \
  -f /database/tests/delivery_serial.sql \
  -f /database/seed_survey_demo.sql -f /database/tests/survey_router.sql
```

tests/invariants.sql, flows.sql и survey_router.sql делают ROLLBACK. **delivery_serial.sql намеренно делает COMMIT между claims**, затем освобождает исключительно свои тестовые lease: он доказывает durable mutex после COMMIT и блокировку uncertain, а не только непересечение message_id. У него нет сетевых вызовов. Выполнять только на test DB; fixture-сообщения остаются для изучения.

Native-тесты требуют два независимых серверных соединения. Они не имеют статуса PASS в этой сессии:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# Попросит пароль korsik_app; использует только выделенную test DB.
python tests/run_native.py --allow-write
```

По умолчанию helper подключается к 127.0.0.1:55432/korsik_test; PGHOST/PGPORT/PGDATABASE/PGUSER переопределяют эти значения. Не задавайте рабочую/корпоративную БД. Тест проверяет concurrent activation, два scheduler, два sender, сохранение serialization после COMMIT/uncertain и concurrent inbox duplicate через месяц. Fixture-данные остаются. Нужны тесты реального API с двумя HR, MAX callbacks, lease timeout и рабочими версиями моделей.

Демонстрация новых ответов:

```bash
docker compose exec db psql -X -h 127.0.0.1 -U korsik_migrator -d korsik_test -W \
  -v ON_ERROR_STOP=1 -c 'SET ROLE korsik_owner' \
  -f /database/walkthrough.sql -f /database/walkthrough_survey.sql
```

Она сохраняет девять типизированных ответов, включая фото-ID, и pending jobs анализа. Отправка API симулируется; файлы по demo:asset не существуют, настоящая модель не вызывается. Двенадцать итоговых вопросов остаются в будущем событии по probation_end_date.

## Upgrade выпущенной v1

Поддерживается ровно схема из прежнего комплекта с schema_migrations=[1,2]. Произвольная изменённая командная БД/старые onboarding_* данные не объявляются совместимыми. Перед рабочим переносом получить реальный DDL/профиль, согласовать mapping и rehearsal.

1. Остановить writers, scheduler, sender и model workers старой версии. Снять backup и проверить его восстановление в копии.
2. Сверить schema_migrations. Найти сотрудников с несколькими активными формами или sending/uncertain; решить их явно, не теряя ответы. Миграция откажется при таких конфликтах и откатит свой DDL.
3. Поместить migrations/003_survey_router.sql в каталог, уже смонтированный существующим контейнером как /database. Либо запустить локальный psql с файлом; не направлять новый Compose на старый volume вслепую.
4. В существующем контейнере применить **только**:

```bash
docker compose exec db psql -X -h 127.0.0.1 -U korsik_migrator -d korsik_demo -W \
  -v ON_ERROR_STOP=1 -c 'SET ROLE korsik_owner' \
  -f /database/migrations/003_survey_router.sql
```

5. Проверить сохранность старых responses/times/invitations и версию 3. Обновить SQLAlchemy metadata и adapter: claim_outbox теперь возвращает 7 полей, queue_message принимает payload; message_analysis имеет другой PK и бизнес-unique; новые typed поля нужно отобразить.
6. Настроить реальные model/policy versions; выполнить SQL-тесты на отдельной копии; переключить backend. Сначала один sender с global_serial=true, затем постепенное включение после native-проверок. Полный Excel shared-режим — только с проверенным типизированным каталогом/asset IDs/календарём.

Миграция содержит lock_timeout и transaction advisory lock, CREATE INDEX/ALTER TABLE и backfill. Это **не zero-downtime migration**; на живых больших таблицах она блокирует работу и требует окна. Она не запускает внешний MAX/Qdrant и не меняет записанные legacy ratings/comments/statuses/times. У старых анализов сохраняется явно помеченный legacy_summary: отсутствующий исторический full HRResult восстановить невозможно.

Повтор migration 003 отклоняется по версии. install.sql нельзя повторять поверх установленной схемы. Безопасный downgrade не поставляется: он потерял бы новые ответы/контекст; откат — согласованный restore/cutover. Пронумерованные миграции не редактируются после принятия релиза. Новые индексы большой рабочей БД могут потребовать отдельного CONCURRENTLY вне transaction block, а не механической замены этого install-файла.

## Worker и мониторинг

- Несколько scheduler допустимы; prepare_due_events с row locks/SKIP LOCKED готовит очередь короткой транзакцией.
- До согласования один delivery worker/concurrency=1 и claim_outbox(1,true). Sending/uncertain останавливают глобальную отправку; это осознанная цена строгого временного режима.
- После серверных тестов возможно per-employee claim_outbox(limit,false): по одному сообщению сотруднику, частичный UNIQUE защищает и после COMMIT. Массовый batch всё равно должен укладываться в lease; пример берёт одно сообщение.
- Inbox: claim + бизнес-изменения/done атомарно; Redis/Celery могут уведомлять, но PostgreSQL регулярно опрашивается.
- RAG: claim/commit → модели → guarded result. Analysis: claim_message_analysis/commit → process_hr_input → apply_hr_analysis. Не делать SQL-транзакцию вокруг сети.
- Периодически recover_leases, recover_rag_leases, recover_analysis_leases, expire_pending_interactions.
- Мониторить pending/dead/uncertain и их возраст, истёкшие lease, unconfigured analysis, задержку отправки относительно send_until, active forms/deadlines, pending clarifications, awaiting_hr, RAG not_found против error, размер/WAL/locks/autovacuum.

Реальные TLS/pg_hba/secret manager, SSO, webhook authentication, callback limits, API idempotency/receipts и file backend требуют настройки. localhost sslmode=disable в примере не переносится на корпоративный сервер: там нужен утверждённый TLS, обычно verify-full. Роль приложения не получает пароли HR и не индексирует их ответы в общий Qdrant.

## Backup, restore, остановка

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U postgres -d korsik_demo -Fc --no-owner > backups/korsik.dump
docker compose exec -T db createdb -U postgres korsik_restore
docker compose exec -T db pg_restore -U postgres -d korsik_restore --no-owner --exit-on-error < backups/korsik.dump
docker compose exec -T db psql -X -U postgres -d korsik_restore \
  -c 'SELECT * FROM korsik.schema_migrations ORDER BY version' \
  -c 'SELECT count(*) FROM korsik.event_answers'
docker compose down
# Сохранившийся named volume будет использован при следующем запуске:
docker compose up -d --wait db
```

Restore выше проверяет данные, а --no-owner не восстанавливает исходное владение: для настоящего восстановления роли/ownership/grants восстанавливаются отдельным runbook. pg_dump не включает глобальные роли и не заменяет backup оригинальных файлов/Qdrant/PITR. Rehearsal должен проверять FK, числа ответов, цитаты, blob hashes, replay индекса и RPO/RTO без настоящей рассылки.

`docker compose down -v` **удалит named volume со всеми БД этого проекта**. Для обычного перезапуска не используется. Retention 24 месяца — только прежнее допущение; автоматических DELETE/DROP нет. Inbox payload можно редактировать в redacted object, сохраняя глобальный event_id/result. Удаление чата связано с reply/case/source/analysis FK и требует отдельного согласованного архивирования. Партиций нет; partition_maintenance.sql только измеряет размеры/статистику.

Официальные основания: [PostgreSQL image](https://hub.docker.com/_/postgres), [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/), [Compose down](https://docs.docker.com/reference/cli/docker/compose/down/), [PostgreSQL backup](https://www.postgresql.org/docs/17/backup-dump.html).
