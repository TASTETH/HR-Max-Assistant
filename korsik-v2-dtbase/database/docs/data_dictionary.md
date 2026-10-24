# Физическая модель и словарь данных

Схема `korsik`, PostgreSQL 17, v2 / migration 003. Все определения ниже получены из каталога после фактического исполнения DDL. Полный исполняемый источник — `schema.sql` и `operations.sql`.

Все FK используют `ON DELETE RESTRICT` и `ON UPDATE NO ACTION`. Решение → запрет удаления связанной истории и каскадного изменения идентичности → HR нельзя удалить вместе с его сотрудниками, а документ нельзя удалить вместе с цитатами → архивирование/обезличивание выполняется отдельной согласованной процедурой. Приложение не получает DELETE/TRUNCATE.

NULL означает отсутствие значения, а не пустую строку. `NOT NULL` и проверки пустых строк указаны независимо: CHECK сам по себе допускает UNKNOWN при NULL. `timestamptz` ниже — timestamp with time zone; `time` — time without time zone.

## `hr_users`

**Задача:** Учётная запись HR/администратора кабинета, привязанная к субъекту SSO.

**Почему не объединено:** Это сотрудник кабинета, а не адаптируемый сотрудник MAX; права доступа не равны должности.

**Кто ссылается:** `audit_log`, `employee_plans`, `employees`, `hr_cases`, `messages`, `plan_versions`

**Основные запросы:** Вход по auth_subject, проверка enabled, назначение закреплённого HR.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `auth_subject` | `text` | нет | `—` |
| `display_name` | `text` | нет | `—` |
| `access_role` | `text` | нет | `'hr'::text` |
| `enabled` | `boolean` | нет | `true` |
| `contact_uri` | `text` | да | `—` |
| `contact_verified_at` | `timestamptz` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((access_role = ANY (ARRAY['hr'::text, 'admin'::text])));
UNIQUE (auth_subject);
CHECK (((contact_uri IS NULL) = (contact_verified_at IS NULL)));
CHECK ((contact_uri ~ '^https://[^[:space:]]+$'::text));
CHECK ((btrim(display_name) <> ''::text));
PRIMARY KEY (id);
```


**Индексы: решения и цена**

- `hr_users_auth_subject_key`: решение → `(auth_subject)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `hr_users_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `org_units`

**Задача:** Подразделение сотрудника и граница доступа к документам.

**Почему не объединено:** Общий FK устраняет расхождения названий отделов в кадровой карточке и ACL документов; иерархия отделов не нужна.

**Кто ссылается:** `documents`, `employees`

**Основные запросы:** Отдел по code, список доступных подразделений, область документов.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `code` | `text` | нет | `—` |
| `name` | `text` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((code ~ '^[A-Z0-9_-]+$'::text));
UNIQUE (code);
CHECK ((btrim(name) <> ''::text));
PRIMARY KEY (id);
```


**Индексы: решения и цена**

- `org_units_code_key`: решение → `(code)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `org_units_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `employees`

**Задача:** Единственная карточка человека: закреплённый HR, кадровые поля, связь MAX и состояние регистрации.

**Почему не объединено:** Регистрация встроена в карточку: это фиксированная анкета ФИО/телефон, а не независимая сущность с другой идентичностью. Много планов/сообщений нельзя хранить в одной строке.

**Кто ссылается:** `audit_log`, `employee_plans`, `hr_cases`, `invitations`, `max_inbox`, `message_analysis`, `messages`, `pending_interactions`

**Основные запросы:** Список HR, проверка анкеты, поиск по аккаунту/чату MAX, блокировка карточки при изменении процесса.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `hr_id` | `bigint` | нет | `—` |
| `org_unit_id` | `bigint` | да | `—` |
| `external_employee_id` | `character varying(100)` | да | `—` |
| `full_name` | `text` | да | `—` |
| `phone` | `text` | да | `—` |
| `job_title` | `text` | да | `—` |
| `start_date` | `date` | да | `—` |
| `timezone` | `text` | нет | `'Europe/Moscow'::text` |
| `lifecycle` | `text` | нет | `'invited'::text` |
| `registration_step` | `text` | нет | `'full_name'::text` |
| `registration_revision` | `integer` | нет | `0` |
| `submitted_at` | `timestamptz` | да | `—` |
| `hr_verified_at` | `timestamptz` | да | `—` |
| `max_account_id` | `text` | да | `—` |
| `max_chat_id` | `text` | да | `—` |
| `created_at` | `timestamptz` | нет | `clock_timestamp()` |
| `location_tag` | `text` | да | `—` |
| `employee_type` | `text` | да | `—` |
| `communication_style` | `text` | да | `—` |
| `probation_end_date` | `date` | да | `—` |
| `context_revision` | `bigint` | нет | `0` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK (((max_account_id IS NULL) = (max_chat_id IS NULL)));
CHECK (((submitted_at IS NULL) OR ((full_name IS NOT NULL) AND (phone IS NOT NULL))));
CHECK (((registration_step = 'submitted'::text) = (submitted_at IS NOT NULL)));
CHECK (((hr_verified_at IS NULL) OR ((submitted_at IS NOT NULL) AND (org_unit_id IS NOT NULL) AND (job_title IS NOT NULL) AND (start_date IS NOT NULL))));
CHECK (((lifecycle <> ALL (ARRAY['ready'::text, 'adapting'::text, 'completed'::text])) OR (hr_verified_at IS NOT NULL)));
CHECK (((probation_end_date IS NULL) OR (start_date IS NULL) OR (probation_end_date >= start_date)));
CHECK ((communication_style = ANY (ARRAY['informal'::text, 'formal'::text])));
CHECK ((context_revision >= 0));
CHECK ((employee_type = ANY (ARRAY['office'::text, 'production'::text])));
UNIQUE (external_employee_id);
CHECK ((btrim(full_name) <> ''::text));
FOREIGN KEY (hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT;
CHECK ((btrim(job_title) <> ''::text));
CHECK ((lifecycle = ANY (ARRAY['invited'::text, 'registering'::text, 'hr_review'::text, 'ready'::text, 'adapting'::text, 'completed'::text, 'stopped'::text])));
CHECK ((location_tag = ANY (ARRAY['GrPP'::text, 'GTP'::text, 'Luzhniki'::text])));
UNIQUE (max_account_id);
UNIQUE (max_chat_id);
FOREIGN KEY (org_unit_id) REFERENCES korsik.org_units(id) ON DELETE RESTRICT;
CHECK ((phone ~ '^\+[1-9][0-9]{7,14}$'::text));
PRIMARY KEY (id);
CHECK ((registration_revision >= 0));
CHECK ((registration_step = ANY (ARRAY['full_name'::text, 'phone'::text, 'confirm'::text, 'submitted'::text])));
```

- Решение → `FOREIGN KEY (hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `employees` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (org_unit_id) REFERENCES korsik.org_units(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `employees` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `employees_external_employee_id_key`: решение → `(external_employee_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `employees_hr_list`: решение → `hr_id, id` (обычный B-tree) → WHERE hr_id=$1 AND id>$cursor ORDER BY id. HR идёт первым как равенство, id обеспечивает устойчивый курсор. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `employees_hr_review`: решение → `hr_id, id` (частичный) → Тот же список только WHERE lifecycle='hr_review'. Частичный индекс не хранит остальные состояния. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `employees_max_account_id_key`: решение → `(max_account_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `employees_max_chat_id_key`: решение → `(max_chat_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `employees_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `invitations`

**Задача:** Секретное приглашение с истечением, отзывом и однократной активацией.

**Почему не объединено:** У человека может быть несколько исторических приглашений. Хранить их одним набором колонок у сотрудника означало бы терять историю отзывов.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Поиск по SHA-256, одно неотозванное неиспользованное приглашение, активация под блокировкой.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `employee_id` | `bigint` | нет | `—` |
| `token_hash` | `bytea` | нет | `—` |
| `created_at` | `timestamptz` | нет | `clock_timestamp()` |
| `expires_at` | `timestamptz` | нет | `—` |
| `revoked_at` | `timestamptz` | да | `—` |
| `activated_at` | `timestamptz` | да | `—` |
| `activated_account_id` | `text` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((expires_at > created_at));
CHECK (((activated_at IS NULL) = (activated_account_id IS NULL)));
CHECK ((NOT ((revoked_at IS NOT NULL) AND (activated_at IS NOT NULL))));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
PRIMARY KEY (id);
CHECK ((octet_length(token_hash) = 32));
UNIQUE (token_hash);
```

- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `invitations` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `invitations_one_live`: решение → `employee_id` (частичный) → Не более одного неотозванного неиспользованного приглашения. Истёкшее приглашение перед переизданием надо отозвать: текущее время не используется в предикате индекса. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `invitations_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `invitations_token_hash_key`: решение → `(token_hash)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `plan_templates`

**Задача:** Название и идентичность повторно используемого плана; необязательная должность для предложения.

**Почему не объединено:** Название шаблона живёт дольше конкретной версии; отдельные позиции/роли доступа сюда не переносятся.

**Кто ссылается:** `plan_versions`

**Основные запросы:** Ручной каталог; простое предложение по suggested_job_title с подтверждением HR.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `code` | `text` | нет | `—` |
| `name` | `text` | нет | `—` |
| `suggested_job_title` | `text` | да | `—` |
| `archived` | `boolean` | нет | `false` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
UNIQUE (code);
PRIMARY KEY (id);
```


**Индексы: решения и цена**

- `plan_templates_code_key`: решение → `(code)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `plan_templates_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `plan_versions`

**Задача:** Нумерованная версия содержимого шаблона с моментом публикации.

**Почему не объединено:** Нужна неизменная ссылка на назначенный вариант; одно изменяемое описание у шаблона разрушило бы историю.

**Кто ссылается:** `employee_plans`, `template_events`

**Основные запросы:** Черновик, публикация, выбор фиксированной опубликованной версии.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `template_id` | `bigint` | нет | `—` |
| `version_no` | `integer` | нет | `—` |
| `published_at` | `timestamptz` | да | `—` |
| `created_by` | `bigint` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
FOREIGN KEY (created_by) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT;
PRIMARY KEY (id);
FOREIGN KEY (template_id) REFERENCES korsik.plan_templates(id) ON DELETE RESTRICT;
UNIQUE (template_id, version_no);
CHECK ((version_no > 0));
```

- Решение → `FOREIGN KEY (created_by) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `plan_versions` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (template_id) REFERENCES korsik.plan_templates(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `plan_versions` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `plan_versions_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `plan_versions_template_id_version_no_key`: решение → `(template_id, version_no)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `template_events`

**Задача:** Типизированные строки конкретной версии плана и относительные сроки.

**Почему не объединено:** Это набор событий, а не набор колонок или произвольный JSON; у версии их несколько.

**Кто ссылается:** `plan_events`, `template_events`

**Основные запросы:** Все строки версии в position, проверка зависимостей перед публикацией.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `version_id` | `bigint` | нет | `—` |
| `code` | `text` | нет | `—` |
| `position` | `integer` | нет | `—` |
| `kind` | `text` | нет | `—` |
| `body` | `text` | нет | `—` |
| `offset_months` | `integer` | нет | `0` |
| `offset_days` | `integer` | нет | `0` |
| `local_time` | `time` | нет | `'09:00:00'::time without time zone` |
| `response_hours` | `integer` | нет | `72` |
| `required` | `boolean` | нет | `true` |
| `reminder_hours` | `integer` | нет | `24` |
| `max_reminders` | `integer` | нет | `2` |
| `depends_on_id` | `bigint` | да | `—` |
| `definition` | `jsonb` | нет | `'{}'::jsonb` |
| `schedule_anchor` | `text` | нет | `'start_date'::text` |
| `workday_no` | `smallint` | да | `—` |
| `send_window_hours` | `integer` | нет | `72` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((btrim(body) <> ''::text));
CHECK (((depends_on_id IS NULL) OR (depends_on_id <> id)));
CHECK (((workday_no IS NULL) OR ((offset_months = 0) AND (offset_days = 0))));
CHECK (korsik.valid_definition(definition, kind));
FOREIGN KEY (depends_on_id, version_id) REFERENCES korsik.template_events(id, version_id) ON DELETE RESTRICT;
UNIQUE (id, version_id);
CHECK ((kind = ANY (ARRAY['info'::text, 'task'::text, 'rating'::text, 'feedback'::text, 'survey'::text])));
CHECK (((max_reminders >= 0) AND (max_reminders <= 10)));
CHECK (((offset_days >= '-30'::integer) AND (offset_days <= 730)));
CHECK (((offset_months >= 0) AND (offset_months <= 24)));
PRIMARY KEY (id);
CHECK (("position" > 0));
CHECK ((reminder_hours > 0));
CHECK (((response_hours >= 1) AND (response_hours <= 8760)));
CHECK ((schedule_anchor = ANY (ARRAY['start_date'::text, 'probation_end_date'::text])));
CHECK (((send_window_hours >= 1) AND (send_window_hours <= 8760)));
UNIQUE (version_id, code);
FOREIGN KEY (version_id) REFERENCES korsik.plan_versions(id) ON DELETE RESTRICT;
UNIQUE (version_id, "position");
CHECK (((workday_no >= 1) AND (workday_no <= 366)));
```

- Решение → `FOREIGN KEY (depends_on_id, version_id) REFERENCES korsik.template_events(id, version_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `template_events` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (version_id) REFERENCES korsik.plan_versions(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `template_events` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `template_events_id_version_id_key`: решение → `(id, version_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `template_events_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `template_events_version_id_code_key`: решение → `(version_id, code)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `template_events_version_id_position_key`: решение → `(version_id, "position")` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `employee_plans`

**Задача:** Факт назначения фиксированной версии одному сотруднику с датой/часовым поясом.

**Почему не объединено:** Назначение не равно шаблону: есть отдельный жизненный цикл, история замен и собственное начало.

**Кто ссылается:** `plan_events`

**Основные запросы:** Один активный план, исторические планы сотрудника, переназначение.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `employee_id` | `bigint` | нет | `—` |
| `version_id` | `bigint` | нет | `—` |
| `assigned_by` | `bigint` | нет | `—` |
| `start_date` | `date` | нет | `—` |
| `timezone` | `text` | нет | `—` |
| `status` | `text` | нет | `'active'::text` |
| `assigned_at` | `timestamptz` | нет | `clock_timestamp()` |
| `ended_at` | `timestamptz` | да | `—` |
| `probation_end_date` | `date` | да | `—` |
| `location_tag` | `text` | да | `—` |
| `employee_type` | `text` | да | `—` |
| `work_calendar` | `jsonb` | нет | `'{"holidays": [], "weekdays": [1, 2, 3, 4, 5], "extra_workdays": []}'::jsonb` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
FOREIGN KEY (assigned_by) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT;
CHECK (((status = 'active'::text) = (ended_at IS NULL)));
CHECK (((probation_end_date IS NULL) OR (probation_end_date >= start_date)));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
CHECK ((employee_type = ANY (ARRAY['office'::text, 'production'::text])));
UNIQUE (id, employee_id, version_id);
CHECK ((location_tag = ANY (ARRAY['GrPP'::text, 'GTP'::text, 'Luzhniki'::text])));
PRIMARY KEY (id);
CHECK ((status = ANY (ARRAY['active'::text, 'completed'::text, 'cancelled'::text])));
FOREIGN KEY (version_id) REFERENCES korsik.plan_versions(id) ON DELETE RESTRICT;
CHECK (korsik.valid_calendar(work_calendar));
```

- Решение → `FOREIGN KEY (assigned_by) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `employee_plans` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `employee_plans` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (version_id) REFERENCES korsik.plan_versions(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `employee_plans` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `employee_plans_id_employee_id_version_id_key`: решение → `(id, employee_id, version_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `employee_plans_one_active`: решение → `employee_id` (частичный) → Ищет актуальный план и запрещает второй active даже в двух конкурентных транзакциях. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `employee_plans_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `plan_events`

**Задача:** Снимки конкретных событий с абсолютными сроками, ожиданиями и ответом.

**Почему не объединено:** События одного плана выполняются независимо. Ответ 1:1 здесь же: отдельная onboarding_answers для этих четырёх простых типов не нужна. Переписка с несколькими сообщениями остаётся отдельно.

**Кто ссылается:** `event_answers`, `hr_cases`, `message_analysis`, `messages`, `plan_events`

**Основные запросы:** Наступившие события, просрочка, напоминания, кнопка конкретного задания, низкие оценки, прогресс.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `plan_id` | `bigint` | нет | `—` |
| `employee_id` | `bigint` | нет | `—` |
| `version_id` | `bigint` | нет | `—` |
| `template_event_id` | `bigint` | нет | `—` |
| `code` | `text` | нет | `—` |
| `position` | `integer` | нет | `—` |
| `kind` | `text` | нет | `—` |
| `body` | `text` | нет | `—` |
| `offset_months` | `integer` | нет | `—` |
| `offset_days` | `integer` | нет | `—` |
| `local_time` | `time` | нет | `—` |
| `response_hours` | `integer` | нет | `—` |
| `required` | `boolean` | нет | `—` |
| `reminder_hours` | `integer` | нет | `—` |
| `max_reminders` | `integer` | нет | `—` |
| `depends_on_id` | `bigint` | да | `—` |
| `scheduled_at` | `timestamptz` | нет | `—` |
| `deadline_at` | `timestamptz` | да | `—` |
| `next_reminder_at` | `timestamptz` | да | `—` |
| `reminders_sent` | `integer` | нет | `0` |
| `status` | `text` | нет | `'scheduled'::text` |
| `rating` | `smallint` | да | `—` |
| `comment` | `text` | да | `—` |
| `task_confirmed_at` | `timestamptz` | да | `—` |
| `answered_at` | `timestamptz` | да | `—` |
| `completed_at` | `timestamptz` | да | `—` |
| `definition` | `jsonb` | нет | `'{}'::jsonb` |
| `current_question_code` | `text` | да | `—` |
| `interaction_revision` | `bigint` | нет | `0` |
| `schedule_anchor` | `text` | нет | `'start_date'::text` |
| `workday_no` | `smallint` | да | `—` |
| `send_window_hours` | `integer` | нет | `72` |
| `send_until_at` | `timestamptz` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((btrim(body) <> ''::text));
CHECK (((depends_on_id IS NULL) OR (depends_on_id <> id)));
CHECK ((deadline_at > scheduled_at));
CHECK (((current_question_code IS NULL) OR (kind = 'survey'::text)));
CHECK ((send_until_at >= scheduled_at));
CHECK (((rating IS NULL) OR (kind = 'rating'::text)));
CHECK (((comment IS NULL) OR (kind = ANY (ARRAY['rating'::text, 'feedback'::text]))));
CHECK (((task_confirmed_at IS NULL) OR (kind = 'task'::text)));
CHECK (((status = 'completed'::text) = (completed_at IS NOT NULL)));
CHECK (((status <> 'awaiting_comment'::text) OR (kind = 'survey'::text) OR ((kind = 'rating'::text) AND (rating IS NOT NULL) AND (rating < 8) AND (comment IS NULL))));
CHECK (((status <> ALL (ARRAY['awaiting_answer'::text, 'awaiting_comment'::text])) OR (deadline_at IS NOT NULL)));
CHECK (((workday_no IS NULL) OR ((offset_months = 0) AND (offset_days = 0))));
CHECK ((korsik.valid_definition(definition, kind) AND (NOT (definition ? 'variants'::text))));
CHECK ((btrim(comment) <> ''::text));
CHECK (((status <> 'completed'::text) OR (kind = ANY (ARRAY['info'::text, 'survey'::text])) OR ((kind = 'rating'::text) AND (rating IS NOT NULL) AND ((rating >= 8) OR (comment IS NOT NULL))) OR ((kind = 'task'::text) AND (task_confirmed_at IS NOT NULL)) OR ((kind = 'feedback'::text) AND (comment IS NOT NULL))));
FOREIGN KEY (depends_on_id, plan_id) REFERENCES korsik.plan_events(id, plan_id) ON DELETE RESTRICT;
UNIQUE (id, employee_id);
UNIQUE (id, plan_id);
CHECK ((interaction_revision >= 0));
CHECK ((kind = ANY (ARRAY['info'::text, 'task'::text, 'rating'::text, 'feedback'::text, 'survey'::text])));
CHECK (((max_reminders >= 0) AND (max_reminders <= 10)));
CHECK (((offset_days >= '-30'::integer) AND (offset_days <= 730)));
CHECK (((offset_months >= 0) AND (offset_months <= 24)));
PRIMARY KEY (id);
UNIQUE (plan_id, code);
FOREIGN KEY (plan_id, employee_id, version_id) REFERENCES korsik.employee_plans(id, employee_id, version_id) ON DELETE RESTRICT;
CHECK (("position" > 0));
CHECK (((rating >= 1) AND (rating <= 10)));
CHECK ((reminder_hours > 0));
CHECK (((reminders_sent >= 0) AND (reminders_sent <= 10)));
CHECK (((response_hours >= 1) AND (response_hours <= 8760)));
CHECK ((schedule_anchor = ANY (ARRAY['start_date'::text, 'probation_end_date'::text])));
CHECK (((send_window_hours >= 1) AND (send_window_hours <= 8760)));
CHECK ((status = ANY (ARRAY['scheduled'::text, 'queued'::text, 'awaiting_answer'::text, 'awaiting_comment'::text, 'completed'::text, 'overdue'::text, 'skipped'::text, 'cancelled'::text])));
FOREIGN KEY (template_event_id, version_id) REFERENCES korsik.template_events(id, version_id) ON DELETE RESTRICT;
CHECK (((workday_no >= 1) AND (workday_no <= 366)));
```

- Решение → `FOREIGN KEY (depends_on_id, plan_id) REFERENCES korsik.plan_events(id, plan_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `plan_events` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (plan_id, employee_id, version_id) REFERENCES korsik.employee_plans(id, employee_id, version_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `plan_events` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (template_event_id, version_id) REFERENCES korsik.template_events(id, version_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `plan_events` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `plan_events_deadline`: решение → `deadline_at, id` (частичный) → Просрочки только ещё открытых queued/awaiting событий; завершённые не занимают этот индекс. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `plan_events_due`: решение → `scheduled_at, id` (частичный) → WHERE status='scheduled' AND scheduled_at<=now() ORDER BY scheduled_at,id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `plan_events_id_employee_id_key`: решение → `(id, employee_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `plan_events_id_plan_id_key`: решение → `(id, plan_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `plan_events_low_ratings`: решение → `answered_at, id` (частичный) → WHERE rating<8 AND answered_at >= $from AND answered_at < $to. Период является первым диапазоном, id развязывает равное время. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `plan_events_one_interactive`: решение → `employee_id` (частичный) → Частичный UNIQUE для queued/awaiting форм; запрещает второй интерактивный процесс, info исключены. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `plan_events_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `plan_events_plan_id_code_key`: решение → `(plan_id, code)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `plan_events_reminders`: решение → `next_reminder_at, id` (частичный) → Напоминания только для двух типов ожидания; сравнение срока и устойчивый порядок. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `plan_events_send_expiry`: решение → `send_until_at,id` (частичный) → Scheduled/queued: актуальность первоначальной отправки, отдельно от окна ответа. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `max_inbox`

**Задача:** Надёжный входящий конверт MAX и глобальный реестр идемпотентности.

**Почему не объединено:** Не каждый webhook является сообщением: есть активация, callback и receipt. Результат обработки/lease нельзя свести к тексту переписки.

**Кто ссылается:** `messages`

**Основные запросы:** INSERT ON CONFLICT(event_id), pending worker, восстановление истёкшей аренды, чтение сохранённого result.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `event_id` | `text` | нет | `—` |
| `employee_id` | `bigint` | да | `—` |
| `received_at` | `timestamptz` | нет | `clock_timestamp()` |
| `payload` | `jsonb` | нет | `—` |
| `status` | `text` | нет | `'pending'::text` |
| `attempts` | `integer` | нет | `0` |
| `next_attempt_at` | `timestamptz` | нет | `clock_timestamp()` |
| `lease_until` | `timestamptz` | да | `—` |
| `generation` | `bigint` | нет | `0` |
| `result` | `jsonb` | да | `—` |
| `last_error` | `text` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (event_id)` → PK(event_id) — настоящий стабильный ID события провайдера в одном подключении MAX; не ID сообщения и не время доставки. Если MAX не даёт стабильного event ID, адаптер обязан согласовать детерминированную идентичность по документированному протоколу; случайный UUID на каждую доставку запрещён.

**Ограничения (точные определения):**

```sql
CHECK ((attempts >= 0));
CHECK (((status = 'processing'::text) = (lease_until IS NOT NULL)));
CHECK (((status <> 'done'::text) OR (result IS NOT NULL)));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
CHECK ((btrim(event_id) <> ''::text));
CHECK ((generation >= 0));
CHECK (((jsonb_typeof(payload) = 'object'::text) AND (octet_length((payload)::text) <= 65536)));
PRIMARY KEY (event_id);
CHECK (((result IS NULL) OR (jsonb_typeof(result) = 'object'::text)));
CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'done'::text, 'dead'::text])));
```

- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `max_inbox` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `max_inbox_leases`: решение → `lease_until` (частичный) → Восстановление только processing с истёкшей арендой. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `max_inbox_pending`: решение → `next_attempt_at, event_id` (частичный) → Ближайший pending webhook для обработки; исключает уже done и DLQ. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `max_inbox_pkey`: решение → `(event_id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `messages`

**Задача:** Единая переписка с точным автором, явными связями контекста и состоянием RAG-вопроса.

**Почему не объединено:** История сообщения и попытка его доставки — разные сроки жизни. Оценки/завершение задания — типизированное состояние события, а не произвольный текст.

**Кто ссылается:** `answer_sources`, `event_answers`, `hr_cases`, `message_analysis`, `messages`, `outbox`, `pending_interactions`

**Основные запросы:** Курсор переписки, контекст обращения/ответа, незавершённые RAG-запросы.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `employee_id` | `bigint` | нет | `—` |
| `direction` | `text` | нет | `—` |
| `author` | `text` | нет | `—` |
| `author_hr_id` | `bigint` | да | `—` |
| `body` | `text` | нет | `—` |
| `deduplication_key` | `text` | да | `—` |
| `inbox_event_id` | `text` | да | `—` |
| `reply_to_id` | `bigint` | да | `—` |
| `event_id` | `bigint` | да | `—` |
| `case_id` | `bigint` | да | `—` |
| `rag_status` | `text` | нет | `'none'::text` |
| `rag_model_version` | `text` | да | `—` |
| `rag_attempts` | `integer` | нет | `0` |
| `rag_generation` | `bigint` | нет | `0` |
| `rag_lease_until` | `timestamptz` | да | `—` |
| `rag_next_attempt_at` | `timestamptz` | нет | `clock_timestamp()` |
| `rag_last_error` | `text` | да | `—` |
| `created_at` | `timestamptz` | нет | `clock_timestamp()` |
| `payload` | `jsonb` | нет | `'{}'::jsonb` |
| `analysis_gate` | `text` | нет | `'none'::text` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((analysis_gate = ANY (ARRAY['none'::text, 'pending'::text, 'clear'::text, 'hr'::text])));
CHECK ((author = ANY (ARRAY['employee'::text, 'bot'::text, 'hr'::text, 'system'::text])));
FOREIGN KEY (author_hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT;
FOREIGN KEY (case_id, employee_id) REFERENCES korsik.hr_cases(id, employee_id) ON DELETE RESTRICT;
CHECK (((author = 'employee'::text) = (direction = 'in'::text)));
CHECK (((author = 'hr'::text) = (author_hr_id IS NOT NULL)));
CHECK (((author <> 'hr'::text) OR (case_id IS NOT NULL)));
CHECK (((direction = 'out'::text) = (deduplication_key IS NOT NULL)));
CHECK (((direction <> 'out'::text) OR (inbox_event_id IS NULL)));
CHECK (((rag_status = 'running'::text) = (rag_lease_until IS NOT NULL)));
CHECK (((btrim(body) <> ''::text) OR ((payload ->> 'kind'::text) = 'action'::text) OR (COALESCE(jsonb_array_length((payload -> 'attachments'::text)), 0) > 0)));
UNIQUE (deduplication_key);
CHECK ((direction = ANY (ARRAY['in'::text, 'out'::text])));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
FOREIGN KEY (event_id, employee_id) REFERENCES korsik.plan_events(id, employee_id) ON DELETE RESTRICT;
FOREIGN KEY (case_id, employee_id, author_hr_id) REFERENCES korsik.hr_cases(id, employee_id, hr_id) ON DELETE RESTRICT;
UNIQUE (id, employee_id);
FOREIGN KEY (inbox_event_id) REFERENCES korsik.max_inbox(event_id) ON DELETE RESTRICT;
UNIQUE (inbox_event_id);
CHECK (korsik.valid_message_payload(payload));
PRIMARY KEY (id);
CHECK ((rag_attempts >= 0));
CHECK ((rag_generation >= 0));
CHECK ((rag_status = ANY (ARRAY['none'::text, 'pending'::text, 'running'::text, 'answered'::text, 'not_found'::text, 'error'::text, 'handed_to_hr'::text])));
FOREIGN KEY (reply_to_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT;
```

- Решение → `FOREIGN KEY (author_hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (case_id, employee_id) REFERENCES korsik.hr_cases(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (event_id, employee_id) REFERENCES korsik.plan_events(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (case_id, employee_id, author_hr_id) REFERENCES korsik.hr_cases(id, employee_id, hr_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (inbox_event_id) REFERENCES korsik.max_inbox(event_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (reply_to_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `messages` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `messages_case`: решение → `case_id, id` (частичный) → Сообщения обращения в порядке id; NULL без обращения исключены. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `messages_conversation`: решение → `employee_id, created_at DESC, id DESC` (обычный B-tree) → WHERE employee_id=$1 AND (created_at,id)<($cursor_time,$cursor_id) ORDER BY created_at DESC,id DESC LIMIT 50. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `messages_deduplication_key_key`: решение → `(deduplication_key)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `messages_id_employee_id_key`: решение → `(id, employee_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `messages_inbox_event_id_key`: решение → `(inbox_event_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `messages_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `messages_rag_leases`: решение → `rag_lease_until, id` (частичный) → Восстановление running RAG после падения. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `messages_rag_pending`: решение → `rag_next_attempt_at, id` (частичный) → Pending/error RAG до восьми попыток; устойчивый порядок захвата. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `messages_reply`: решение → `reply_to_id, id` (частичный) → Все ответы на один вопрос; проверка/отмена ожидающего RAG-ответа при передаче HR. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → переписка до расчётных 7,3 млн строк за 24 месяца; сначала измеряем размер/планы запросов → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `hr_cases`

**Задача:** Один вопрос, переданный закреплённому HR, и его состояние.

**Почему не объединено:** У сотрудника бывает несколько независимых вопросов; case связывает продолжения и блокирует автоматический ответ только по этому вопросу. Очереди свободных операторов нет.

**Кто ссылается:** `message_analysis`, `messages`

**Основные запросы:** Требуется ответ, открытые обращения своего HR, root_message_id, thread.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `employee_id` | `bigint` | нет | `—` |
| `hr_id` | `bigint` | нет | `—` |
| `root_message_id` | `bigint` | нет | `—` |
| `reason` | `text` | нет | `—` |
| `context_summary` | `text` | нет | `—` |
| `status` | `text` | нет | `'awaiting_hr'::text` |
| `created_at` | `timestamptz` | нет | `clock_timestamp()` |
| `closed_at` | `timestamptz` | да | `—` |
| `source_key` | `text` | нет | `—` |
| `source_event_id` | `bigint` | да | `—` |
| `source_question_code` | `text` | да | `—` |
| `category` | `text` | да | `—` |
| `priority` | `text` | нет | `'medium'::text` |
| `closure_basis` | `text` | да | `—` |
| `closure_note` | `text` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK (((status = 'closed'::text) = (closed_at IS NOT NULL)));
CHECK (((source_question_code IS NULL) OR (source_event_id IS NOT NULL)));
CHECK ((closure_basis = ANY (ARRAY['verified_delivery'::text, 'api_accepted'::text])));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
FOREIGN KEY (hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT;
UNIQUE (id, employee_id, hr_id);
UNIQUE (id, employee_id);
PRIMARY KEY (id);
CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text])));
CHECK ((reason = ANY (ARRAY['explicit_human'::text, 'no_answer'::text, 'problem'::text, 'manual'::text])));
FOREIGN KEY (root_message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT;
UNIQUE (root_message_id);
FOREIGN KEY (source_event_id, employee_id) REFERENCES korsik.plan_events(id, employee_id) ON DELETE RESTRICT;
FOREIGN KEY (source_event_id, source_question_code) REFERENCES korsik.event_answers(event_id, question_code) ON DELETE RESTRICT;
CHECK ((status = ANY (ARRAY['awaiting_hr'::text, 'answered'::text, 'queued'::text, 'sent'::text, 'delivered'::text, 'closed'::text])));
```

- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `hr_cases` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `hr_cases` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (root_message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `hr_cases` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (source_event_id, employee_id) REFERENCES korsik.plan_events(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `hr_cases` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (source_event_id, source_question_code) REFERENCES korsik.event_answers(event_id, question_code) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `hr_cases` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `hr_cases_answer_source`: решение → `source_event_id,source_question_code` (частичный) → Контекст обращения по вопросу опроса, NULL-источники исключены. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `hr_cases_id_employee_id_hr_id_key`: решение → `(id, employee_id, hr_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `hr_cases_id_employee_id_key`: решение → `(id, employee_id)` (обычный B-tree) → UNIQUE составной цели FK: id уже уникален по PK, но PostgreSQL нужен именно этот ключ для проверки владельца/версии/плана одной связью. Это сознательная дополнительная структура, а не второй индекс на один id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `hr_cases_one_open_source`: решение → `source_key` (частичный) → Частичный UNIQUE открытого обращения для одной проблемы/ответа, независимо от разных сообщений/моделей. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `hr_cases_open`: решение → `hr_id, created_at, id` (частичный) → Открытые обращения одного HR; hr_id — равенство, затем хронология. Индикатор ответа дополнительно фильтрует awaiting_hr. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `hr_cases_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `hr_cases_root_message_id_key`: решение → `(root_message_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `outbox`

**Задача:** Одна запись доставки для одного исходящего сообщения: lease, retry, принятие API, receipt.

**Почему не объединено:** Входящие сообщения не отправляются; внешний I/O и повторные попытки не должны менять исторический текст. Таблица попыток на MVP не нужна: счётчик и последняя ошибка достаточны.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Pending отправки, получение lease, повтор после явного отказа, uncertain, подтверждение доставки.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `message_id` | `bigint` | нет | `—` |
| `status` | `text` | нет | `'pending'::text` |
| `purpose` | `text` | нет | `—` |
| `attempts` | `integer` | нет | `0` |
| `generation` | `bigint` | нет | `0` |
| `next_attempt_at` | `timestamptz` | нет | `clock_timestamp()` |
| `lease_until` | `timestamptz` | да | `—` |
| `last_error` | `text` | да | `—` |
| `external_message_id` | `text` | да | `—` |
| `accepted_at` | `timestamptz` | да | `—` |
| `delivered_at` | `timestamptz` | да | `—` |
| `employee_id` | `bigint` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (message_id)` → PK(message_id) одновременно FK на messages: одно исходящее сообщение имеет одну запись логической доставки, несколько попыток меняют attempts/generation.

**Ограничения (точные определения):**

```sql
CHECK ((attempts >= 0));
CHECK (((status = 'sending'::text) = (lease_until IS NOT NULL)));
CHECK (((accepted_at IS NULL) = (external_message_id IS NULL)));
CHECK (((status <> ALL (ARRAY['accepted'::text, 'delivered'::text])) OR (accepted_at IS NOT NULL)));
CHECK (((status = 'delivered'::text) = (delivered_at IS NOT NULL)));
UNIQUE (external_message_id);
CHECK ((generation >= 0));
FOREIGN KEY (message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT;
FOREIGN KEY (message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT;
PRIMARY KEY (message_id);
CHECK ((purpose = ANY (ARRAY['event'::text, 'reminder'::text, 'hr_reply'::text, 'rag'::text, 'notice'::text])));
CHECK ((status = ANY (ARRAY['pending'::text, 'sending'::text, 'accepted'::text, 'delivered'::text, 'uncertain'::text, 'dead'::text, 'cancelled'::text])));
```

- Решение → `FOREIGN KEY (message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `outbox` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `outbox` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `outbox_external_message_id_key`: решение → `(external_message_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `outbox_leases`: решение → `lease_until, message_id` (частичный) → Проверка истёкших sending; перевод в uncertain вместо слепого повтора. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `outbox_one_inflight_per_employee`: решение → `employee_id` (частичный) → Частичный UNIQUE sending/uncertain; durable mutex после COMMIT. Дополнительный FK проверяет совпадение с владельцем сообщения. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `outbox_pending`: решение → `next_attempt_at, message_id` (частичный) → Наступившие pending отправки по сроку retry; успешно отправленные не хранятся в этом индексе. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `outbox_pkey`: решение → `(message_id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `documents`

**Задача:** Идентичность корпоративного документа и простая область доступа.

**Почему не объединено:** Название/доступ относятся ко всему документу; версии содержимого и поисковые фрагменты имеют отдельную историю.

**Кто ссылается:** `document_versions`

**Основные запросы:** Документ по code; enabled и scope_org_unit_id перед новым ответом.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `code` | `text` | нет | `—` |
| `title` | `text` | нет | `—` |
| `scope_org_unit_id` | `bigint` | да | `—` |
| `enabled` | `boolean` | нет | `true` |
| `scope_location_tag` | `text` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
UNIQUE (code);
PRIMARY KEY (id);
CHECK ((scope_location_tag = ANY (ARRAY['GrPP'::text, 'GTP'::text, 'Luzhniki'::text])));
FOREIGN KEY (scope_org_unit_id) REFERENCES korsik.org_units(id) ON DELETE RESTRICT;
```

- Решение → `FOREIGN KEY (scope_org_unit_id) REFERENCES korsik.org_units(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `documents` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `documents_code_key`: решение → `(code)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `documents_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `document_versions`

**Задача:** Версия файла с неизменным объектным адресом, хешем, сроком действия и готовностью индекса.

**Почему не объединено:** Документ изменяется; источник ответа должен навсегда указывать конкретную версию, а не текущий файл по изменяемому URL.

**Кто ссылается:** `document_chunks`

**Основные запросы:** Одна is_current на документ, актуальность, index_status, восстановление источника старого ответа.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `document_id` | `bigint` | нет | `—` |
| `version_no` | `integer` | нет | `—` |
| `object_uri` | `text` | нет | `—` |
| `sha256` | `bytea` | нет | `—` |
| `valid_from` | `timestamptz` | нет | `—` |
| `valid_until` | `timestamptz` | да | `—` |
| `is_current` | `boolean` | нет | `false` |
| `index_status` | `text` | нет | `'pending'::text` |
| `indexed_at` | `timestamptz` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK (((valid_until IS NULL) OR (valid_until > valid_from)));
CHECK (((NOT is_current) OR (index_status = 'ready'::text)));
CHECK (((index_status <> 'ready'::text) OR (indexed_at IS NOT NULL)));
FOREIGN KEY (document_id) REFERENCES korsik.documents(id) ON DELETE RESTRICT;
UNIQUE (document_id, version_no);
CHECK ((index_status = ANY (ARRAY['pending'::text, 'ready'::text, 'error'::text])));
PRIMARY KEY (id);
CHECK ((octet_length(sha256) = 32));
CHECK ((version_no > 0));
```

- Решение → `FOREIGN KEY (document_id) REFERENCES korsik.documents(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `document_versions` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `document_versions_current`: решение → `document_id` (частичный) → Запрещает два is_current для одного документа; действительность по времени проверяется при операции. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `document_versions_document_id_version_no_key`: решение → `(document_id, version_no)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `document_versions_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `document_chunks`

**Задача:** Стабильный фрагмент версии, его текст, locator и Qdrant point ID.

**Почему не объединено:** На одну версию приходится несколько фрагментов. Текст нужен для воспроизводимой цитаты; векторов в PostgreSQL нет.

**Кто ссылается:** `answer_sources`

**Основные запросы:** Фрагменты версии по ordinal; Qdrant UUID → фрагмент и версия.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `version_id` | `bigint` | нет | `—` |
| `ordinal` | `integer` | нет | `—` |
| `locator` | `text` | нет | `—` |
| `content` | `text` | нет | `—` |
| `qdrant_point_id` | `uuid` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((btrim(content) <> ''::text));
CHECK ((ordinal > 0));
PRIMARY KEY (id);
UNIQUE (qdrant_point_id);
FOREIGN KEY (version_id) REFERENCES korsik.document_versions(id) ON DELETE RESTRICT;
UNIQUE (version_id, ordinal);
```

- Решение → `FOREIGN KEY (version_id) REFERENCES korsik.document_versions(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `document_chunks` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `document_chunks_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `document_chunks_qdrant_point_id_key`: решение → `(qdrant_point_id)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `document_chunks_version_id_ordinal_key`: решение → `(version_id, ordinal)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `answer_sources`

**Задача:** Связь исходящего ответа с отобранными фрагментами и порядком цитирования.

**Почему не объединено:** Связь многие-ко-многим: один ответ использует несколько фрагментов, один фрагмент используется многими ответами.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Источники ответа по message_id в rank; проверка источников перед отправкой.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `message_id` | `bigint` | нет | `—` |
| `chunk_id` | `bigint` | нет | `—` |
| `rank` | `integer` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (message_id, chunk_id)` → PK(message_id,chunk_id) — сама связь; отдельный id не добавил бы смысла и всё равно потребовал бы UNIQUE этой пары.

**Ограничения (точные определения):**

```sql
FOREIGN KEY (chunk_id) REFERENCES korsik.document_chunks(id) ON DELETE RESTRICT;
FOREIGN KEY (message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT;
UNIQUE (message_id, rank);
PRIMARY KEY (message_id, chunk_id);
CHECK ((rank > 0));
```

- Решение → `FOREIGN KEY (chunk_id) REFERENCES korsik.document_chunks(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `answer_sources` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `answer_sources` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `answer_sources_message_id_rank_key`: решение → `(message_id, rank)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `answer_sources_pkey`: решение → `(message_id, chunk_id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `message_analysis`

**Задача:** Устойчивая работа HR Intelligence: точный вход, версии, lease, полный результат, ошибка и связь с case.

**Почему не объединено:** Расширена существующая таблица анализа, а не создан параллельный analysis_jobs. Один message/model недостаточен для комментария и policy revision.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Claim по model/policy/time, recovery leases, reanalysis input_revision, полный HRResult и аналитика категории.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `message_id` | `bigint` | нет | `—` |
| `model_version` | `text` | нет | `—` |
| `topic` | `text` | да | `—` |
| `sentiment` | `text` | да | `—` |
| `problem_level` | `smallint` | да | `—` |
| `confidence` | `numeric(5,4)` | да | `—` |
| `analyzed_at` | `timestamptz` | да | `clock_timestamp()` |
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `employee_id` | `bigint` | нет | `—` |
| `source_key` | `text` | нет | `—` |
| `input_revision` | `integer` | нет | `0` |
| `policy_version` | `text` | нет | `'legacy'::text` |
| `source_event_id` | `bigint` | да | `—` |
| `source_question_code` | `text` | да | `—` |
| `request` | `jsonb` | нет | `'{}'::jsonb` |
| `result` | `jsonb` | да | `—` |
| `status` | `text` | нет | `'done'::text` |
| `attempts` | `integer` | нет | `0` |
| `generation` | `bigint` | нет | `0` |
| `lease_until` | `timestamptz` | да | `—` |
| `next_attempt_at` | `timestamptz` | нет | `clock_timestamp()` |
| `last_error` | `text` | да | `—` |
| `case_id` | `bigint` | да | `—` |
| `category` | `text` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → BIGINT IDENTITY идентифицирует отдельное lease-задание/результат. Дополнительный UNIQUE(source_key,input_revision,model_version,policy_version) запрещает дубли бизнес-анализа; это осознанная смена PK v1, требующая обновить SQLAlchemy/upsert.

**Ограничения (точные определения):**

```sql
CHECK ((attempts >= 0));
FOREIGN KEY (case_id, employee_id) REFERENCES korsik.hr_cases(id, employee_id) ON DELETE RESTRICT;
CHECK (((status = 'running'::text) = (lease_until IS NOT NULL)));
CHECK (((status <> 'done'::text) OR (result IS NOT NULL)));
CHECK (((source_question_code IS NULL) OR (source_event_id IS NOT NULL)));
CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
CHECK ((generation >= 0));
CHECK ((input_revision >= 0));
FOREIGN KEY (message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT;
FOREIGN KEY (message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT;
PRIMARY KEY (id);
CHECK (((problem_level >= 0) AND (problem_level <= 3)));
CHECK (((jsonb_typeof(request) = 'object'::text) AND (octet_length((request)::text) <= 65536)));
CHECK (((jsonb_typeof(result) = 'object'::text) AND (octet_length((result)::text) <= 131072)));
CHECK ((sentiment = ANY (ARRAY['positive'::text, 'neutral'::text, 'negative'::text, 'mixed'::text, 'unknown'::text])));
FOREIGN KEY (source_event_id, employee_id) REFERENCES korsik.plan_events(id, employee_id) ON DELETE RESTRICT;
FOREIGN KEY (source_event_id, source_question_code) REFERENCES korsik.event_answers(event_id, question_code) ON DELETE RESTRICT;
UNIQUE (source_key, input_revision, model_version, policy_version);
CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, 'error'::text, 'dead'::text, 'superseded'::text])));
```

- Решение → `FOREIGN KEY (case_id, employee_id) REFERENCES korsik.hr_cases(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `message_analysis` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `message_analysis` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `message_analysis` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `message_analysis` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (source_event_id, employee_id) REFERENCES korsik.plan_events(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `message_analysis` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (source_event_id, source_question_code) REFERENCES korsik.event_answers(event_id, question_code) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `message_analysis` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `message_analysis_leases`: решение → `lease_until,id` (частичный) → Recovery истёкших running заданий. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `message_analysis_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `message_analysis_source`: решение → `source_event_id,source_question_code,input_revision` (обычный B-tree) → История анализа одного бизнес-ответа и версия комментария. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `message_analysis_source_key_input_revision_model_version_po_key`: решение → `(source_key, input_revision, model_version, policy_version)` (обычный B-tree) → UNIQUE: проверка уникальности указанного бизнес-ключа или поддержка точной составной цели FK; поиск использует левую часть указанного порядка. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `message_analysis_work`: решение → `model_version,policy_version,next_attempt_at,id` (частичный) → Работа только выбранных фактических версий модели/политики, pending/error по времени retry. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `audit_log`

**Задача:** Небольшой добавляемый журнал значимых бизнес-действий.

**Почему не объединено:** Не совпадает с чатом, webhook или техническим логом: фиксирует кто/когда назначил план, проверил анкету, отправил ответ.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Хронология действия и история сотрудника; не предназначен для всех вызовов LLM.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `occurred_at` | `timestamptz` | нет | `clock_timestamp()` |
| `employee_id` | `bigint` | да | `—` |
| `actor_hr_id` | `bigint` | да | `—` |
| `action` | `text` | нет | `—` |
| `details` | `jsonb` | нет | `'{}'::jsonb` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → `bigint GENERATED ALWAYS AS IDENTITY` даёт компактную внутреннюю идентичность → backend сохраняет id созданной строки и передаёт его в FK → номера могут иметь пропуски после rollback; ID не секрет и не разрешение доступа. UNIQUE бизнес-полей задаётся отдельно.

**Ограничения (точные определения):**

```sql
CHECK ((btrim(action) <> ''::text));
FOREIGN KEY (actor_hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT;
CHECK (((jsonb_typeof(details) = 'object'::text) AND (octet_length((details)::text) <= 8192)));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
PRIMARY KEY (id);
```

- Решение → `FOREIGN KEY (actor_hr_id) REFERENCES korsik.hr_users(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `audit_log` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `audit_log` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `audit_log_employee`: решение → `employee_id, occurred_at, id` (обычный B-tree) → Значимые действия для конкретного сотрудника в хронологическом порядке. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `audit_log_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.
- `audit_log_time`: решение → `occurred_at, id` (обычный B-tree) → Журнал за период и курсор по времени/id. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → аудит до расчётных 365 тыс. строк за 24 месяца → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `schema_migrations`

**Задача:** Служебная отметка применённых версий DDL.

**Почему не объединено:** Не бизнес-таблица; не смешивается с изменениями кадровой карточки и не доступна роли приложения.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Проверка версии схемы при старте backend и планирование следующей миграции.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `version` | `integer` | нет | `—` |
| `applied_at` | `timestamptz` | нет | `clock_timestamp()` |
| `description` | `text` | нет | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (version)` → PK(version) — номер миграции, заданный разработчиком, а не сгенерированное бизнес-ID.

**Ограничения (точные определения):**

```sql
PRIMARY KEY (version);
```


**Индексы: решения и цена**

- `schema_migrations_pkey`: решение → `(version)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `event_answers`

**Задача:** Бизнес-ответы нескольких вопросов одного survey, включая draft и комментарий.

**Почему не объединено:** Одна строка plan_events не может вместить девять независимых типизированных ответов; inbox не является бизнес-хранилищем. Legacy простые ответы не дублируются.

**Кто ссылается:** `hr_cases`, `message_analysis`

**Основные запросы:** event/question, низкие оценки по периоду, прогресс формы, источник HR-анализа.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `event_id` | `bigint` | нет | `—` |
| `question_code` | `text` | нет | `—` |
| `answer_type` | `text` | нет | `—` |
| `state` | `text` | нет | `—` |
| `revision` | `integer` | нет | `0` |
| `rating` | `smallint` | да | `—` |
| `value` | `jsonb` | да | `—` |
| `comment` | `text` | да | `—` |
| `answered_at` | `timestamptz` | да | `—` |
| `commented_at` | `timestamptz` | да | `—` |
| `source_message_id` | `bigint` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (event_id, question_code)` → Составной PK(event_id,question_code) идентифицирует один бизнес-ответ вопроса. Существование кода/типа в immutable definition проверяет trigger; revision сохраняет версию комментария для анализа, отдельный ID не нужен.

**Ограничения (точные определения):**

```sql
CHECK ((answer_type = ANY (ARRAY['rating'::text, 'boolean'::text, 'single_choice'::text, 'multi_choice'::text, 'text'::text, 'attachment'::text])));
CHECK (((state = 'draft'::text) = (answered_at IS NULL)));
CHECK (((comment IS NULL) = (commented_at IS NULL)));
CHECK (((state <> 'awaiting_comment'::text) OR (comment IS NULL)));
CHECK (((state = 'skipped'::text) OR ((answer_type = 'rating'::text) AND (rating IS NOT NULL) AND (value IS NULL)) OR ((answer_type = 'boolean'::text) AND (rating IS NULL) AND (jsonb_typeof(value) = 'boolean'::text)) OR ((answer_type = 'single_choice'::text) AND (rating IS NULL) AND (jsonb_typeof(value) = 'string'::text)) OR ((answer_type = 'multi_choice'::text) AND (rating IS NULL) AND (jsonb_typeof(value) = 'array'::text)) OR ((answer_type = 'text'::text) AND (rating IS NULL) AND (jsonb_typeof(value) = 'string'::text) AND ((length(btrim((value #>> '{}'::text[]))) >= 1) AND (length(btrim((value #>> '{}'::text[]))) <= 8000))) OR ((answer_type = 'attachment'::text) AND (rating IS NULL) AND korsik.json_keys_allowed(value, ARRAY['asset_ref'::text, 'kind'::text]) AND (jsonb_typeof((value -> 'asset_ref'::text)) = 'string'::text) AND ((length((value ->> 'asset_ref'::text)) >= 1) AND (length((value ->> 'asset_ref'::text)) <= 500)) AND ((value ->> 'kind'::text) = ANY (ARRAY['photo'::text, 'image'::text, 'pdf'::text, 'file'::text])))));
CHECK (((state = 'skipped'::text) OR (((answer_type = 'rating'::text) = (rating IS NOT NULL)) AND ((answer_type = 'rating'::text) OR (value IS NOT NULL)))));
CHECK (((state <> 'draft'::text) OR (answer_type = 'multi_choice'::text)));
CHECK (((length(btrim(comment)) >= 1) AND (length(btrim(comment)) <= 8000)));
FOREIGN KEY (event_id) REFERENCES korsik.plan_events(id) ON DELETE RESTRICT;
PRIMARY KEY (event_id, question_code);
CHECK (((rating >= 1) AND (rating <= 10)));
CHECK ((revision >= 0));
FOREIGN KEY (source_message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT;
CHECK ((state = ANY (ARRAY['draft'::text, 'awaiting_comment'::text, 'accepted'::text, 'skipped'::text])));
```

- Решение → `FOREIGN KEY (event_id) REFERENCES korsik.plan_events(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `event_answers` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (source_message_id) REFERENCES korsik.messages(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `event_answers` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `event_answers_low`: решение → `answered_at,event_id,question_code` (частичный) → Низкие оценки по периоду в survey; legacy индекс plan_events сохраняется для legacy рейтинга. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `event_answers_pkey`: решение → `(event_id, question_code)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.

## `pending_interactions`

**Задача:** Устойчивое уточнение назначения исходного сообщения.

**Почему не объединено:** У уточнения свой срок, варианты, revision и одно финальное решение, отличные от исходного inbox.done.

**Кто ссылается:** Других FK на эту таблицу нет.

**Основные запросы:** Pending сотрудника; callback по id; восстановление после restart; immutable resolved.

| Поле | PostgreSQL-тип | NULL | Значение по умолчанию |
|---|---|---|---|
| `id` | `bigint` | нет | `GENERATED ALWAYS AS IDENTITY` |
| `employee_id` | `bigint` | нет | `—` |
| `original_message_id` | `bigint` | нет | `—` |
| `context_revision` | `bigint` | нет | `—` |
| `choices` | `jsonb` | нет | `—` |
| `expires_at` | `timestamptz` | нет | `—` |
| `created_at` | `timestamptz` | нет | `clock_timestamp()` |
| `status` | `text` | нет | `'pending'::text` |
| `chosen_code` | `text` | да | `—` |
| `resolution` | `jsonb` | да | `—` |
| `resolved_at` | `timestamptz` | да | `—` |

**PK. Решение → зачем → пример → цена:** `PRIMARY KEY (id)` → BIGINT IDENTITY — устойчивый ID уточнения в callback. FK связывает original_message_id с тем же employee. Один pending и один resolved на оригинал обеспечены разными частичными UNIQUE.

**Ограничения (точные определения):**

```sql
CHECK ((expires_at > created_at));
CHECK (((status = 'resolved'::text) = (resolved_at IS NOT NULL)));
CHECK (((status <> 'resolved'::text) OR ((chosen_code IS NOT NULL) AND (resolution IS NOT NULL))));
CHECK (((status = 'resolved'::text) OR ((chosen_code IS NULL) AND (resolution IS NULL))));
CHECK (((jsonb_typeof(choices) = 'array'::text) AND ((jsonb_array_length(choices) >= 2) AND (jsonb_array_length(choices) <= 10)) AND (octet_length((choices)::text) <= 8192)));
CHECK ((context_revision >= 0));
FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT;
FOREIGN KEY (original_message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT;
PRIMARY KEY (id);
CHECK ((jsonb_typeof(resolution) = 'object'::text));
CHECK ((status = ANY (ARRAY['pending'::text, 'resolved'::text, 'expired'::text, 'cancelled'::text])));
```

- Решение → `FOREIGN KEY (employee_id) REFERENCES korsik.employees(id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `pending_interactions` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.
- Решение → `FOREIGN KEY (original_message_id, employee_id) REFERENCES korsik.messages(id, employee_id) ON DELETE RESTRICT` → связь не может указывать на неизвестную/чужую строку → для `pending_interactions` операция допустима только при наличии указанного родителя (в составном FK одновременно совпадает владелец/версия/план) → дополнительные колонки составной связи должны передаваться вместе; удаление родителя запрещено, а FK не создаёт индекс на дочерних колонках.

**Индексы: решения и цена**

- `pending_interactions_active`: решение → `employee_id,expires_at,id` (частичный) → Список живых уточнений сотрудника и TTL. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `pending_interactions_one_pending`: решение → `original_message_id` (частичный) → Частичный UNIQUE одного pending snapshot оригиналу. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `pending_interactions_one_result`: решение → `original_message_id` (частичный) → Частичный UNIQUE одного resolved результата оригиналу; истёкшие snapshots остаются историей. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа; смена статуса добавляет/удаляет запись из частичного индекса. Не дублирует PK/UNIQUE: другой порядок/набор колонок или предикат.
- `pending_interactions_pkey`: решение → `(id)` (обычный B-tree) → PK: быстрый поиск и join по идентичности строки. → пример: запросы этой таблицы выше и в `queries.sql` → цена: место, WAL и работа при INSERT/изменении ключа. Отдельный индекс поверх PK/UNIQUE для тех же колонок не создаётся.

**Партиционирование:** решение → обычная таблица → сохраняет простые PK/FK и глобальные уникальности → объём этой сущности и её запросы не оправдывают отдельные партиции → удаление больших объёмов требует согласованных пакетных операций; автоматического DROP нет.
