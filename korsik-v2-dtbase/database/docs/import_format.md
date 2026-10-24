# Контракт сценария и импорта v2

Исходный Excel не импортируется автоматически. Действуют только установленная типизированная модель и предварительный просмотр. Нельзя объявлять поддержку всех строк Excel только потому, что добавлен JSONB.

## Строка события

| Поле | Правило |
|---|---|
| code/position | Уникальны в template version; position >0 |
| kind | info/task/rating/feedback/survey |
| text | Непустой текст, importer ограничивает каналом (предложение 4000) |
| schedule_anchor | start_date/probation_end_date |
| workday_no | NULL или 1..366; если задан, offsets обязаны быть нулевыми |
| offset_months/offset_days | 0..24 и -30..730, только при отсутствии workday_no |
| local_time | HH:MM локально сотруднику |
| send_window_hours | 1..8760, начальная актуальность отправки; cap пребординга — начало даты выхода |
| response_hours | 1..8760, от первого принятия API |
| required | TRUE/FALSE, обязательность события для завершения плана |
| reminder_hours/max_reminders | Положительный interval и 0..10; для info в importer 0 |
| depends_on_code | Только более раннее событие той же версии, без цикла |
| definition | JSON object по закрытому контракту ниже, {} для простого legacy-события |

Для `.xlsx` предлагается один лист events с такими заголовками, definition — JSON-текст одной ячейки. Альтернатива UI загружает уже нормализованный JSON; не разные неявные Excel-диалекты. Метаданные template/version HR вводит отдельно. Календарь/пробная дата берутся из подтверждённых HR данных, не догадкой из job_title.

## definition schema_version=1

Допустимые ключи: schema_version, questions, audience, variants, completion_text, attachments, options. Общий лимит 64 KiB, до 50 вопросов и 20 вариантов. Для survey обязателен непустой questions. Для info/task/rating/feedback questions не допускается. На персональном событии variants запрещён: уже выбран один snapshot.

Вопрос: code, type, prompt; optional required (default true), analyze (default false), options, exclusive_options, comment_rule, when, scale_direction, reply_text. Нет произвольных действий/скриптов/SQL/переходов. Типы rating/boolean/single_choice/multi_choice/text/attachment. У rating обязателен low_bad; ниже 8 требуется комментарий. choice IDs устойчивы и уникальны; labels/ответные reply_text утверждаются HR. Выбранный option reply_text не проходит перефразирование LLM.

comment_rule: {mode:always/never/low_rating/values, values:[...] только для values}. Для rating допустимы low_rating/always. Для boolean список values содержит JSON booleans, для choice — ID опций. Other option с обязательным комментарием покрывает «свой вариант» без универсального конструктора полей.

when: {question_code,operator:equals/contains,value}. Ссылка только на более ранний вопрос; условие проверяется по принятому ответу. Multi-select toggle — draft, отдельный submit — окончательный ответ. exclusive_options, например ["none"], запрещает «Никто» вместе с другими. У optional показывается явный skip; комментарий уже принятого сигнала нельзя молча пропустить.

Аудитория: location_tags=[GrPP/GTP/Luzhniki], employee_types=[office/production]. Top-level audience фильтрует событие, variants выбирает body/questions/attachments/options. Ноль совпадений → общий вариант, больше одного → ошибка. Если площадка неизвестна, не выбирается чужой локальный материал. Следующая версия template не меняет уже выбранные personal snapshots.

Файлы: attachments=[{asset_ref,kind:image/photo/pdf/map/file}], без байтов и секретов. Исходных изображений/подтверждённых asset IDs в материалах нет; demo IDs явно вымышлены. Фото сотрудника не разрешает публикацию в другом канале.

Полный пример девяти вопросов — [survey_definition.json](../examples/survey_definition.json), SQL — seed_survey_demo.sql. Там же генерируется шаблон двенадцати итоговых вопросов для проверки структуры, не объявленный полным утверждённым контентом Excel.

## Preview → apply

Importer проверяет точный набор колонок, диапазоны/JSON-типы/дубли/ссылки, ограничение файла/строк (предложение 5 MiB и 200 событий), отсутствие формул/макросов, варианты аудитории и все утверждённые asset refs. Показывает ошибки по строкам, выбранные вопросы/текст для сотрудника, календарные и рабочие сроки в local/UTC, send_until/deadline preview и зависимости. Частичное применение плохого файла запрещено.

После подтверждения HR: lock template header → новая draft plan_versions → template_events → publication в одной транзакции. Конкурентный номер версии защищает UNIQUE. Назначение — отдельное подтверждение HR. Расхождение B53:B56 и F54 хранится как открытый контентный вопрос; не выбирается «правильная» дата по модели. Утверждённый производственный календарь обязателен для корпоративных рабочих дней.
