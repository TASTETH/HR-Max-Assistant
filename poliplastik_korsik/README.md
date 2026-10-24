# Корсик: OCR + RAG для ПОЛИПЛАСТИК

Python 3.12, Windows PowerShell. OCR v2.1.0 сохранён. RAG читает завершённый
снимок через `ingest.load_current()`. OCR не выполняется при вопросах сотрудников.
Backend в предоставленных файлах отсутствует: поставляется самостоятельный сервис.

Пошаговая проверка без LLM: [PRE_LLM_CHECK.md](PRE_LLM_CHECK.md).
Файлы для Git и локальной работы: [GIT_GUIDE.md](GIT_GUIDE.md).

## 1. Установка

Распакуй проект, открой PowerShell в папке с `ingest.py` и `korsik_rag`.
Нужен установленный Docker Desktop с Linux containers для Qdrant.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements-all.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe scripts\init_env.py
docker compose up -d
.\.venv\Scripts\python.exe -m korsik_rag download-models
```

При первом запуске скачиваются большие веса двух моделей; кэш остаётся в
`data/models`. Для закрытого контура перенеси готовый кэш и выставь
`RAG_OFFLINE=true`. CPU использует float32; FP16 автоматически не включается.
Для CUDA нужны совместимые GPU/драйвер и сборка PyTorch, затем `RAG_DEVICE=cuda`.
CUDA в этой поставке не проверялась.

## 2. Готовый результат OCR → индекс

В `data/ingested` включён завершённый снимок, полученный из ваших документов.
Можно использовать свой проверенный результат: в `.env` измени
`RAG_INGESTION_OUTPUT=C:/Users/Lvlup/Rag_poliplastic/poliplastik_ocr_v2/data/ingested`.
Важно указывать папку с `current.json`, а не отдельный chunks.jsonl.

```powershell
.\.venv\Scripts\python.exe -m korsik_rag index
```

Повторный запуск переиспользует индекс, если корпус и существенные настройки
совпадают и целостность подтверждена. Новая версия строится в новой коллекции и
папке; активный указатель переключается только после успешной проверки. Предыдущая
версия сохраняется при ошибке. После индексации перезапусти API для активации.
Старые коллекции автоматически не удаляются.

При необходимости повторно запусти сохранённый OCR:

```powershell
.\.venv\Scripts\python.exe ingest.py --input "C:\Users\Lvlup\Downloads\POLIPLASTIK.zip" --output "data\ingested" --tesseract-path "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

Нужен Tesseract 5 rus+eng только для повторного OCR. Основной режим, исключения,
SmartArt, кэш, проверки целостности и опциональный `--audit` остались прежними.
Подробности — `OCR_README.md`. Новый chunking не вводился.

## 3. Посмотреть поиск без LLM

```powershell
.\.venv\Scripts\python.exe -m korsik_rag search "Можно ли принять подарок наличными?" --org-id polyplastic_group_llc
.\.venv\Scripts\python.exe -m korsik_rag search "Какой график рабочей недели установлен ПВТР?" --org-id polyplastic_group_llc
```

`polyplastic_group_llc` — локальный идентификатор ООО «Группа ПОЛИПЛАСТИК»,
названного в документах, а не всех организаций группы. CLI — доверенный инструмент
оператора. Для другой организации без разрешённых документов поиск ничего не выдаёт.

Пайплайн: нормализованный BGE-M3 → cosine/Qdrant top-20 + русский BM25 top-20 →
RRF → 12 кандидатов → CrossEncoder → до 5 исходных кандидатов в контекст.
Продолжение правил может добавить страницы/слайды сверх числа исходных кандидатов.
Сохраняются цитаты и все исходные chunk_id. Logits reranker — не вероятность ответа;
порог автоматического «ответ есть» не применяется.

## 4. Подключить одну генерацию LLM

В `.env` заполни адрес OpenAI-compatible chat completions сервера и имя модели:

```dotenv
RAG_LLM_BASE_URL=http://127.0.0.1:8000/v1
RAG_LLM_MODEL=имя-модели-на-вашем-сервере
RAG_LLM_API_KEY=
RAG_LLM_TIMEOUT=90
RAG_LLM_MAX_TOKENS=1200
RAG_LLM_INPUT_MAX_TOKENS=16000
```

URL приведён как пример, готового LLM-сервера в проекте нет. Укажи фактический
лимит контекста своей модели. Рекомендуется заполнить `RAG_LLM_TOKENIZER` и
`RAG_LLM_TOKENIZER_REVISION` токенизатором той же LLM. Без него используется
консервативный бюджет по UTF-8 байтам, который может отклонить помещающийся prompt.
Текст источников не обрезается незаметно. `RAG_LLM_JSON_MODE=true` включай только
если сервер поддерживает `response_format=json_object`. Специфичные параметры
reasoning, temperature и подобные по умолчанию не отправляются.

```powershell
.\.venv\Scripts\python.exe -m korsik_rag ask "Какие условия действуют для деловых подарков?" --org-id polyplastic_group_llc
```

Без адреса/модели LLM команда вернёт `service_error` с `llm_not_configured`, а не
успешный ответ. При ошибке формата/цитат повторная генерация не выполняется.

## 5. Запуск API и запрос от backend

```powershell
.\.venv\Scripts\python.exe -m korsik_rag serve --host 127.0.0.1 --port 8003
```

Один worker, модели и BM25 загружаются один раз. Inference выполняется вне event
loop; одновременно обрабатывается один вычислительный запрос, ожидание ограничено.
Первый старт загружает веса в RAM. При недоступном Qdrant/индексе/моделях readiness
возвращает 503; смотри код ошибки в журнале. Логи содержат request_id, версию,
длительности и ID источников; текст переписки и ключи не записываются.

В другом PowerShell, из той же папки:

```powershell
$token = (Select-String -Path .env -Pattern '^RAG_SERVICE_TOKEN=').Line.Split('=', 2)[1]
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod "http://127.0.0.1:8003/health/ready" -Headers $headers
$body = @{
  question = "Какой график рабочей недели установлен ПВТР?"
  context = @{ org_id = "polyplastic_group_llc"; site_id = $null; permissions = @("kb:read"); can_escalate = $true }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod "http://127.0.0.1:8003/rag/ask" -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

Endpoint — внутренний **service-to-service**. Токен хранится в backend; MAX-клиент
его не получает. Backend сам берёт организацию, площадку и права из своей БД и
передаёт `context`. Не копируй эти значения из текста сообщения сотрудника.
Если backend обращается через сеть, настрой внутренний маршрут/TLS; Compose по
умолчанию открывает Qdrant только на localhost. Endpoint индексации в API отсутствует.

Формат результата: `status`, `answer`, `sources`, `can_escalate`, `request_id`,
при технической ошибке `error_code`. Возможные статусы: `answered`,
`needs_clarification`, `insufficient_evidence`, `service_error`.
`/health/ready` отдельно показывает `llm_configured`: готовность поиска не означает,
что доступность генерации была проверена. Кнопка HR и создание обращения — работа
основного приложения; сервис не имитирует отправленное обращение.

## Прямой Python-интерфейс

Создай `RagService` один раз в lifecycle backend, а не для каждого сообщения:

Если backend использует другое Python-окружение, установи зависимости и этот
проект туда через `pip install -e "C:\путь\к\poliplastik_korsik" --no-deps`.
Переменная `RAG_ENV_FILE` может указывать на полный путь к его `.env`.
Относительные пути данных разрешаются относительно этого файла, а не cwd backend.

```python
from fastapi.concurrency import run_in_threadpool
from korsik_rag.config import Settings
from korsik_rag.service import RagService
from korsik_rag.types import AccessContext

rag = RagService(Settings.from_env())

# Значения поступают из доверенного backend после проверки сотрудника.
context = AccessContext(org_id="polyplastic_group_llc", permissions=["kb:read"])
result = await run_in_threadpool(rag.ask, "Можно ли разделить отпуск?", context)
payload = result.model_dump()
# При завершении приложения: rag.close()
```

История: `rag.ask(question, context, history=[{"role":"user","content":"..."}])`.
Используется ограниченное число последних сообщений. Неоднозначное продолжение
без истории приводит к уточнению, без обязательного LLM-переписывания запроса.

## Проверка и область применения

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests_rag -v
.\.venv\Scripts\python.exe checks\run_search_checks.py
```

Второй скрипт использует проверочный контекст для документов ООО
«Группа ПОЛИПЛАСТИК». Это операторский инструмент, не публичный endpoint.
В `checks/questions.json` — 20 вопросов с цитатами, сверенными по фактическому
тексту: подарки, коррупция, тайна, график, отпуска, отсутствие ответа и диалог.
Результаты выполненных проверок и их границы — `VERIFICATION.md`, `verification/`.

Новые документы требуют явного описания SHA-256 и прав в `document_scope.json`.
Новая площадка не выводится из названия файла или вопроса. При неизвестной
организации возвращается уточнение. ПВТР не переносится на все предприятия группы.
Описание кейса, Excel, отсутствующие приложения и расписания не добавляются в KB.
Подлинность смысла каждого ответа LLM не доказывается проверкой JSON: валидируются
источники, дословные цитаты и формат. Генерацию надо проверить с вашим endpoint.
