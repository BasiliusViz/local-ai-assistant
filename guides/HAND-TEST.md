# Ручная проверка после обновления

Команды по порядку: сначала сервер, потом компьютер пользователя. После
каждого шага написано, что должно получиться. Не совпало — остановиться на
этом шаге и прислать вывод.

---

## На сервере (в папке проекта)

### 1. Подтянуть код

```bash
git pull
```

```bash
git log --oneline -1
```

Должен быть последний коммит из GitHub.

### 2. Пересобрать и пересоздать контейнеры

Код `kb/` лежит внутри образа: без `--build` контейнеры остаются на старом
коде, даже после `git pull`.

```bash
docker compose up -d --build --force-recreate kb dojo cb-graph
```

```bash
docker compose ps kb dojo cb-graph
```

Все три `Up`, в `CREATED` — только что.

### 3. Серверы отвечают и отдают инструменты

```bash
for p in 8010 8011 8012 8013; do echo "== $p"; curl -s -X POST "http://localhost:$p/mcp" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -c 'import sys,json; print(", ".join(t["name"] for t in json.load(sys.stdin)["result"]["tools"]))'; done
```

Должно быть:
- `8010`: `kb_search, code_search, cb_search, jira_search`
- `8011`: `query_graph, get_node, get_neighbors, ...`
- `8012`: `dojo_findings, dojo_engagements, dojo_compare, dojo_release_notes`
- `8013`: `cb_query_graph, cb_get_node, cb_get_neighbors, ...` — с префиксом `cb_`.
  Пусто или ошибка «граф недоступен» — релиз ещё не скачан, см. 6б

### 4. У dojo_findings продукт необязателен

```bash
curl -s -X POST http://localhost:8012/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -c 'import sys,json; s=json.load(sys.stdin)["result"]["tools"][0]["inputSchema"]; print("required:", s.get("required"))'
```

Должно быть `required: None`. Если `['product']` — контейнер `dojo` не
пересобран, вернуться к шагу 2.

### 5. DefectDojo по всем продуктам — так, как вызовет модель

```bash
curl -s -X POST http://localhost:8012/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"dojo_findings","arguments":{}}}' | python3 -c 'import sys,json; r=json.loads(json.load(sys.stdin)["result"]["content"][0]["text"]); print(r.get("error") or r["summary"]); [print(" ", p["product"], "критичных:", p["Critical"], "высоких:", p["High"], "всего:", p["total"]) for p in r.get("by_product", [])]'
```

Должна быть сводка по уровням и список продуктов с числами.

`Находки не проиндексированы` — запустить индексацию:

```bash
./dojo/cron.sh
```

### 6. Поиск по документации отвечает

```bash
curl -s -X POST http://localhost:8010/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"kb_search","arguments":{"query":"password requirements"}}}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["content"][0]["text"][:600])'
```

Должны быть найденные фрагменты, а не ошибка.

### 6а. Jira: эпики, спринты и свои поля

В контейнере новый код выгрузки. Должно быть `2`, `0` — не было `--build`:

```bash
docker compose exec kb grep -c "EPIC_LINK_TYPE" jira/sync.py
```

В выгруженных задачах есть новые поля. Команда берёт первый файл задачи и
печатает его имя и строки с эпиком, спринтами и своими полями:

```bash
docker compose exec kb sh -c 'f=$(find /docs/jira -name "*.json" ! -name ".*" | head -1); echo $f; grep -E "\"(epic|epic_name|sprints|fields)\"" $f'
```

- строки `"epic"`, `"sprints"`, `"fields"` есть (пустые значения — тоже
  норма, если у задачи эпика нет) — файлы новые, сразу к индексации;
- только имя файла — файлы выгружены старым кодом, нужна перевыгрузка:

```bash
docker compose exec kb python jira/sync.py --full
```

В начале вывода — `Доп. поля: эпик, спринт, ...`. `Доп. поля: нет` —
остановиться и прислать вывод: Jira не отдала поля эпика и спринта. Затем:

```bash
docker compose exec kb python -m kb.jira_index /docs/jira --full
```

Эпики попали в индекс — должен быть список названий, а не `[]`:

```bash
docker compose exec kb python -c "from kb import jira_retriever as j; print(j.values('epic_name', limit=20))"
```

### 6б. Код релиза CB18.5 (граф и поиск отдельно от обычного кода)

Список репозиториев релиза — `repos/cb.txt` (образец `repos/cb.example.txt`), в `.env` — `CB_DIR`.
Контейнеры уже пересобраны (шаг 2, вместе с `cb-graph`).

**6б.1. Доступ и ветки** — ничего не скачивает:

```bash
./update-cb.sh --check
```

Все строки `[ok]`.

**6б.2. Скачать** — только код, без графа и индекса. Можно блоками: закомментировать
часть `repos/cb.txt` через `#`, скачать, посмотреть место, открыть следующую часть.
Уже скачанное повторно не качается.

```bash
./update-cb.sh --download
```

В конце — `Место: ... свободно ...`.

**6б.3а. Что возьмётся и что мешает** — по всем репозиториям: взято/отброшено и почему,
по расширениям, каталогам и репозиториям; раздел «Кандидаты» — пропущенные расширения,
которые на деле текст (их можно дописать в `TEXT_SUFFIXES`). Быстро, ничего не пишет:

```bash
docker compose exec -T kb python -m kb.code_survey /cb > ~/cb-survey.txt; less ~/cb-survey.txt
```

Убрали репозитории из `repos/cb.txt` (например, скачанные сторонние библиотеки) — их
папки остаются в `CB_DIR` и попадут в граф и индекс. Показать такие и команду, чтобы
убрать их в сторону (не удаляя):

```bash
./update-cb.sh --orphans
```

**6б.3б. Скорость эмбеддера** — сколько чанков в секунду, и во сколько это выльется
(подставить число чанков из 6б.3). Не запускать одновременно с индексацией:

```bash
docker compose exec -T kb python -m kb.embed_speed --chunks 950000
```

**6б.3. Сколько будет индексироваться** — нарезка без эмбеддингов, быстро, ничего не пишет:

```bash
docker compose exec -T kb python -c "from pathlib import Path; from kb.code_index import collect; print('ИТОГО чанков:', len(collect(Path('/cb'))))"
```

По репозиторию — `--- <репо>: подходящих файлов X из Y, чанков Z`, в конце `ИТОГО чанков`.
Время индексации ≈ ИТОГО / скорость эмбеддера: на стенде ~9 чанков в секунду, то есть
~32 тыс. в час (точнее — по счётчику `x/N` в первые минуты шага 6б.4). Один репозиторий
даёт непропорционально много — проверить, не попали ли туда сгенерированный код или
сторонние библиотеки (дописать каталог в `SKIP_DIRS`, `kb/code_index.py`).

**6б.4. Граф и индекс** — долго, поэтому в фоне, чтобы обрыв SSH не убил:

```bash
nohup ./update-cb.sh > ~/update-cb.log 2>&1 &
```

```bash
tail -f ~/update-cb.log
```

В конце — `Готово`, перед ним `Записано: N`. Оборвалось — запустить ещё раз: граф
пересоберётся, индекс `code_cb` пересоздастся с нуля; обычный код и документы не
затрагиваются.

**6б.5. Индекс на месте:**

```bash
curl -s localhost:6333/collections/code_cb | grep -o '"points_count":[0-9]*'
```

Не ноль. Обычная коллекция `code` при этом не изменилась.

### 7. Автотесты

```bash
docker compose exec kb python -m kb.test_dojo_retriever
```

```bash
docker compose exec kb python -m kb.test_code_chunks
```

```bash
docker compose exec kb python -m kb.test_code_search
```

```bash
docker compose exec cb-graph python /app/test_mcp_prefix.py
```

```bash
docker compose exec kb python -m kb.test_doc_index
```

```bash
docker compose exec kb python -m kb.test_dojo_readonly
```

```bash
docker compose exec kb python confluence/test_sync.py
```

```bash
docker compose exec kb python -m kb.test_jira
```

```bash
docker compose exec kb python jira/test_sync.py
```

В конце каждого `OK`. Тесты с Qdrant заводят временные коллекции и сами их
удаляют — рабочую базу не трогают.

### 7а. Модель правильно выбирает инструменты DefectDojo

```bash
docker compose exec kb python -m kb.dojo_tool_eval --model qwen3.6:35b
```

17 формулировок («сравни…», «release notes…», «какие engagement…»): модель
видит те же инструменты и правила, что в Continue, проверяется, какой
инструмент она выбрала и что положила в поля. DefectDojo не трогается.
Должно быть `Итог: 17 из 17` или близко; `МИМО` — прислать вывод.

### 8. Самопроверка всего стека

```bash
docker compose exec kb python selftest.py --qdrant http://qdrant:6333 --kb http://kb:8010 --dojo http://dojo:8012 --code-graph http://code-graph:8011
```

Код возврата — число провалов, в выводе не должно быть `FAIL`.

---

## На компьютере пользователя — Windows (PowerShell)

### 9. Подтянуть новые правила

В папке, где лежит `setup-continue.ps1`:

```powershell
git pull
```

### 10. Обновить конфиг Continue

Закрыть VS Code. `АДРЕС-СЕРВЕРА` — тот же сервер, где делали шаги 1–8.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup-continue.ps1 -OllamaUrl http://АДРЕС-OLLAMA:11434 -ServerUrl http://АДРЕС-СЕРВЕРА -Model qwen3.6:35b -ContextLength 32768 -SkipExtension
```

Везде `[OK]`, в том числе `8012: dojo_findings`.

### 11. В конфиге правильный адрес

```powershell
Select-String "url:" $HOME\.continue\config.yaml
```

Три строки с адресом сервера, порты 8010, 8011, 8012.

### 12. Схема глазами Continue

```powershell
$r = Invoke-RestMethod -Uri "http://АДРЕС-СЕРВЕРА:8012/mcp" -Method Post -ContentType "application/json" -Headers @{ Accept = "application/json, text/event-stream" } -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'; "required: " + ($r.result.tools[0].inputSchema.required -join ", ")
```

Должно быть `required: ` — пусто.

### 13. VS Code закрыт полностью

Закрыть все окна, затем:

```powershell
Get-Process Code -ErrorAction SilentlyContinue
```

Ничего не должно вывести. Вывело — закрыть VS Code из трея или подождать.

---

## На компьютере пользователя — Linux

### 9. Подтянуть новые правила

```bash
git pull
```

### 10. Обновить конфиг Continue

Закрыть VS Code.

```bash
bash setup-continue.sh --ollama http://АДРЕС-OLLAMA:11434 --server http://АДРЕС-СЕРВЕРА --model qwen3.6:35b --context 32768 --skip-extension
```

Везде `[OK]`.

### 11. В конфиге правильный адрес

```bash
grep "url:" ~/.continue/config.yaml
```

### 12. Схема глазами Continue

```bash
curl -s -X POST http://АДРЕС-СЕРВЕРА:8012/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -c 'import sys,json; print("required:", json.load(sys.stdin)["result"]["tools"][0]["inputSchema"].get("required"))'
```

Должно быть `required: None`.

### 13. VS Code закрыт полностью

```bash
pgrep -l code
```

Ничего не должно вывести.

---

## Проверка в чате (обе системы)

1. Открыть VS Code, панель Continue.
2. **+** — новый чат (старый помнит прежние ответы).
3. Режим **Agent**.
4. Задать по очереди:

| Вопрос | Что должно быть |
|---|---|
| `напиши функцию на Go, которая разворачивает строку` | ответ кодом, без инструментов |
| `поищи в базе, какие требования к паролям` | вызов `kb_search`, ответ со ссылкой на документ |
| `посмотри в коде, где проверяется токен авторизации` | вызов `code_search`, файл и строка |
| `посмотри в релизе, где проверяется токен авторизации` | вызов `cb_search` (не `code_search`), файлы релиза |
| `кто в релизе вызывает <функция из ответа выше>` | вызов `cb_get_neighbors`, не `get_neighbors` |
| `сравни, как проверяется токен у нас и в релизе` | оба: `code_search` и `cb_search` |
| `dojo общая картина` | вызов `dojo_findings` **без** `product`, таблица по продуктам |
| `dojo критичные в <продукт>` | вызов с `product`, находки этого продукта |

Ответ странный — развернуть блок вызова инструмента в чате: там видно, с
какими аргументами модель его позвала и что он вернул. Это прислать.
