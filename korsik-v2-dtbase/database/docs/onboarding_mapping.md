## 7. Соответствие существующему onboarding-модулю

| Сейчас | Итоговая модель | Что менять в SQLAlchemy 2 |
|---|---|---|
| `onboarding_enrollments` | `employee_plans` + одна общая `employees` | Строковый employee_id не PK сотрудника: сохранить как `employees.external_employee_id`; enrollment ссылается на bigint employees.id. start_date/timezone остаются снимком назначения. Уникален один active, а не любой план за всю жизнь |
| `onboarding_steps` | `plan_events` | Добавить employee/version/template origin, тип task, snapshot настроек, deadline/reminder/dependency, расширенные состояния |
| `onboarding_answers` | Типизированные поля `plan_events` | Связь 1:1 слить; rating/comment/answered/comment-complete переносятся в соответствующие поля. Для commented_at при старых данных потребуется согласовать отображение на completed_at, не выдумывать время |
| `onboarding_messages` | `messages` + `outbox` | Текст/автор/ключ операции в messages; sent_at больше не означает receipt. Принятие API отдельно от доставки и retries |
| `onboarding_reply_events` | `max_inbox` и, для текстового входа, `messages` | MAX provider event_id уникален глобально, независимо от месяца. Старый PK(enrollment_id,event_id) надо изучить: одинаковый event_id разных enrollment нельзя автоматически считать дублем. Исторические IDs можно пометить отдельным namespace `legacy:<enrollment>:<event>` с явной картой происхождения |

В репозитории должен остаться **один** набор моделей этих процессов. Здесь не создаются параллельные `onboarding_*`-таблицы.

Без реальных metadata SQLAlchemy и данных это **не безопасная миграция существующих данных**. План согласования: снять DDL/версии пакетов → получить обезличенный профиль данных → проверить коллизии employee_id и event_id → утвердить mapping статусов/времён → создать новую схему в копии → перенести с таблицей соответствий → сверить числа/ответы/ключи → прогнать тесты и rehearsal восстановления → согласовать переключение writer и откат. Alembic-миграция должна отражать утверждённый mapping; одновременная запись в старый и новый модуль без cutover-протокола недопустима.


В v2 новые survey-ответы находятся в event_answers. Старые простые rating/task/feedback продолжают answer_event без второго параллельного onboarding-набора. Поля в message_analysis и callbacks обновляются совместно с backend.
