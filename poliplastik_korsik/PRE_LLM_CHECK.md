# Проверка всей цепочки до генерации LLM

Команды выполняются из `poliplastik_korsik`, где находятся `ingest.py`,
`korsik_rag`, `.env.example` и `docker-compose.yml`.
Они не отправляют запросов генерации LLM.

## Однократная установка, если ещё не выполнена

```powershell
cd "C:\Users\Lvlup\Rag_poliplastic\poliplastik_korsik"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements-all.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe scripts\init_env.py
```

`init_env.py` создаёт `.env` и ключи для Qdrant/API; существующие непустые ключи
сохраняются. Настройки LLM можно оставить пустыми.

## 1. Запуск хранилища и подготовка моделей

Нужен работающий Docker Desktop с Linux containers.

```powershell
docker compose up -d
docker compose ps
.\.venv\Scripts\python.exe -m korsik_rag download-models
```

Первое скачивание весов объёмное. Затем используется локальный кэш моделей.

## 2. Проверка корпуса и согласованного индекса

В поставке уже есть готовый canonical ingestion snapshot. Если используешь свой,
укажи его output в `.env` через `RAG_INGESTION_OUTPUT` (папка с `current.json`).

```powershell
.\.venv\Scripts\python.exe validate_corpus.py --output "data\ingested"
.\.venv\Scripts\python.exe -m korsik_rag index
.\.venv\Scripts\python.exe -m korsik_rag index
```

Первая команда относится к включённому `data/ingested`; при внешнем OCR output
укажи тот же фактический путь, который записан в `.env`.
Ожидаем `all_mechanical_checks_passed: true`. Индексация читает `load_current()`,
проверяет исходные смещения и scope, строит векторы и сверяет набор ID с Qdrant.
При повторе ожидаем `reused: true`. Для текущего корпуса — 131 чанк.

## 3. Одиночный вопрос: dense + BM25 + RRF + reranker + контекст

```powershell
.\.venv\Scripts\python.exe -m korsik_rag search "Можно ли принять подарок наличными?" --org-id polyplastic_group_llc
```

В `candidates` видны результаты после reranker. В `retrieval` каждого кандидата
сохранены позиции и score dense/BM25; `rrf_score` — результат fusion,
`reranker_score` — сырой logit, а `sources` — готовые исходные тексты контекста.

Проверь: в контексте должны быть слайды 8–9 Антикоррупционной политики —
ограничения формы подарка и остальные условия. `status: retrieved` не означает
готового ответа сотруднику: это диагностика поиска.

```powershell
.\.venv\Scripts\python.exe -m korsik_rag search "Какой график рабочей недели установлен ПВТР?" --org-id polyplastic_group_llc
.\.venv\Scripts\python.exe -m korsik_rag search "Можно ли разделить ежегодный отпуск на части?" --org-id polyplastic_group_llc
.\.venv\Scripts\python.exe -m korsik_rag search "Есть ли корпоративная ипотека и какая процентная ставка?" --org-id polyplastic_group_llc
```

Для графика ожидаем основной источник на странице 15 и продолжение условий на
странице 16. Для разделения отпуска — страницу 18, п. 6.7.11.
Для ипотеки кандидаты могут быть: dense всегда ищет ближайшее. В этих документах
нет подтверждения ипотечной программы; корректный финальный отказ проверим с LLM.

Каждая отдельная CLI-команда запускает новый процесс и загружает модели в RAM.
Это не повторное скачивание. Скрипт ниже и API используют один экземпляр сервиса.

## 4. Проверка 20 вопросов одним запуском

```powershell
.\.venv\Scripts\python.exe checks\run_search_checks.py
```

В нашем выполненном прогоне: 17 положительных случаев, включая продолжение
диалога, `positive_context_coverage: 17`, `errors: 0`. Негативные и неоднозначный
вопросы присутствуют в отчёте, но качество отказа LLM этим скриптом не оценивается.
Ожидай несколько минут на CPU. Отчёт: `verification/search_checks.json`.

## 5. API readiness без генерации

В одном окне:

```powershell
.\.venv\Scripts\python.exe -m korsik_rag serve --host 127.0.0.1 --port 8003
```

В другом окне PowerShell, из папки проекта:

```powershell
$token = (Select-String -Path .env -Pattern '^RAG_SERVICE_TOKEN=').Line.Split('=', 2)[1]
Invoke-RestMethod "http://127.0.0.1:8003/health/ready" -Headers @{ Authorization = "Bearer $token" }
```

Ожидаем `ready: true`, `search_ready: true`; без настройки LLM —
`llm_configured: false`. Эта проверка не выполняет генерацию.

Для разбора результата достаточно прислать итог повторной индексации, верхних
кандидатов/источники одиночного вопроса и сводку скрипта 20 вопросов. `.env` и
сервисные ключи присылать не требуется.
