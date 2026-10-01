# План: граф релиза CB18.5 в SQLite + карточки репозиториев

Продолжение `guides/PLAN-CB.md`. Записано 30.09.2026, делать в новой сессии.

> **Сессия 1 написана 30.09.2026** (`code/graph_store.py`, `code/graph_server.py`, тесты
> `code/test_graph_store.py`; проверено на выводе Graphify 0.9.48 по `kb/` и `code/`).
> **На сервере развёрнуто 30.09.2026:** готовые графы загружены без повторного Graphify
> (`graph_store.py build /data ... --jenkins`): 81 репозиторий (все, что в `CB_DIR`) + `_jenkins`,
> 2.25 млн узлов, 51 млн связей; сервер отвечает, отчёт — `./check-cb-graph.sh`. Jenkins: 48
> пайплайнов, шагов общей библиотеки в релизе нет — связей 0. Проверка в Continue — за пользователем.
> Отличия от плана:
> FTS5 не понадобился (точное имя и начало — по индексу, часть имени — LIKE);
> `cb_get_community` не делали — сообщества у каждого репозитория свои.

## Где остановились

- **Поиск по релизу работает:** `cb_search`, коллекция `code_cb`, ~1.6 млн кусков, векторы
  на диске (сжатая копия в памяти). Индекс не трогать — пересчёт 10–15 ч.
- **Графа релиза нет.** Graphify построил граф каждого репозитория
  (`<CB_DIR>/<репо>/graphify-out/graph.json`, уже с `.graphifyignore`: без vendor и тестов),
  но склейка в один граф падает по памяти (код 137): всего графов **3.4 ГБ**, `main` —
  **1.3 ГБ**, остальные до ~0.5 ГБ. Graphify держит граф в networkx (в 5–10 раз больше
  файла), на сервере 15 ГБ на всё. `cb-graph` сейчас поднимает только прослойку и
  отвечает «граф не поднят».
- `code-graph` (обычный код) остаётся на Graphify как есть — он маленький.

## Решение

Графы Graphify по репозиториям → одна база SQLite на диске (без склейки, без networkx) →
свой MCP-сервер с теми же инструментами. Поверх — карточки репозиториев (контекст для
навигации: модель сначала выбирает репозиторий, потом спрашивает граф) и витрина Obsidian.
Сравнение с Neo4j/Memgraph/igraph/Kuzu — в переписке: SQLite встроен в Python, память почти
не нужна, база одним файлом.

## Что известно о данных (Graphify 0.9.48)

- `graph.json` — node-link JSON, ключи: `directed, multigraph, graph, nodes, links, hyperedges`.
- Узел: `id, label, source_file, source_location ("L12"), file_type, community,
  community_name, norm_label, _origin`. **`id` не уникален между репозиториями**
  (`cmd_main`) → ключ в базе `(repo, id)`.
- Связь: `source, target, relation (calls/imports/...), confidence, confidence_score,
  context, source_file, source_location, weight`.
- Схемы инструментов, которые повторяем: `graphify/serve.py`, строки ~1581–1660
  (`query_graph, get_node, get_neighbors, get_community, god_nodes, graph_stats,
  shortest_path`). Колёсико 0.9.48 — `pip download graphifyy==0.9.48 --no-deps`.

## Ограничения

Только stdlib (`sqlite3`, `json`), память — килобайты на поток, все инструменты только на
чтение, `.sh` — LF. Файл 1.3 ГБ нельзя `json.load` целиком — **потоковое чтение**
(`json.JSONDecoder.raw_decode` по элементам массивов `nodes`/`links`).

## Сессия 1 — граф в SQLite

1. `code/graph_store.py` — потоковый читатель node-link JSON + загрузка в SQLite: таблицы
   `nodes(repo,id,label,...)`, `edges(repo,src,dst,relation,...)`, индексы по `(repo,src)`,
   `(repo,dst)`, FTS5 по `label`; по репозиторию за транзакцию; повторный запуск
   перезаписывает репозиторий. CLI `build <CB_DIR> --db <CB_DIR>/graph/graph.sqlite`.
2. `code/graph_server.py` — MCP streamable-http на stdlib (как `mcp_prefix.py`): сразу имена
   `cb_get_node, cb_get_neighbors, cb_query_graph (BFS, лимит глубины), cb_shortest_path
   (с двух концов, лимит 6), cb_graph_stats, cb_god_nodes`, у всех необязательный `repo`;
   описания — по Graphify, с пометкой «РЕЛИЗ CB18.5». Прослойка `mcp_prefix.py` для
   cb-graph больше не нужна (оставить файл — пригодится).
3. `code/cb-entry.sh` — сервер по SQLite; `code/sync.sh` — режим без склейки
   (`GRAPH_MERGE=no`): только `graphify update` по репозиториям + `graph_store build`.
4. `update-cb.sh` шаг 2, `check-cb.py` раздел 4 (графы — из базы), `selftest.py`,
   `continue-rules.yaml` + правило в конфиге (инструменты те же — проверить описания).
5. Тесты: читатель (вложенный `graph`, `hyperedges`, огромный файл кусками, `edges` вместо
   `links`), загрузка, каждый инструмент на крошечной базе, ограничения глубины.
6. Проверить на настоящем выводе Graphify 0.9.48 (временный venv, как 30.09).
7. `code/.dockerignore` — дописать новые файлы (белый список!), `code/Dockerfile` COPY.

Проверка на сервере: `docker compose up -d --build cb-graph` →
`docker compose exec -T cb-graph /app/sync.sh` → размер `graph.sqlite` и время →
`python3 check-cb.py` → Continue: «кто в релизе вызывает X».

## Сессия 2 — карточки репозиториев и Obsidian

> **Написана 30.09.2026, на сервере не запускалась.** `code/repo_cards.py` (+ `test_repo_cards.py`),
> инструмент `cb_repos` в `graph_server.py`, шаг в `sync.sh`, `./update-cb.sh --cards`, правила
> Continue (в том числе `continue-rules.v2.yaml`), `selftest.py`, `check-cb.py` раздел 4.
> Отличия от плана: карточки — `cards.json` рядом с базой (81 запись, Qdrant не нужен); поиск в
> `cb_repos` — по словам (имя, заголовок и текст README, модули, функции), без эмбеддингов;
> фраз от модели нет — на сервере шлюз пускает только `/api/embed`. Если README мало, это
> следующий шаг (генерация на машине с доступом к модели, результат — в `cards.json`).
> Проверка на сервере: `docker compose up -d --build cb-graph` → `./update-cb.sh --cards` →
> `python3 check-cb.py` → Continue: «из каких частей состоит релиз», «что делает alert-manager»,
> «от чего зависит vault-manager». Obsidian — `<CB_DIR>/graph/obsidian`.

1. Карточка на репозиторий: README (первые абзацы), языки и объём (из `code_survey`),
   главные модули и «центральные» функции (из SQLite), зависимости от других репозиториев
   (`go.mod`, `pom.xml`, `package.json`, импорты). По желанию — 2–3 фразы от локальной
   модели (через `/v1`, `reasoning_effort: "none"`).
2. Инструмент `cb_repos(query)` — какие репозитории про это; карточки — тот же `graph.sqlite`
   или маленькая коллекция Qdrant.
3. Выгрузка в Obsidian: заметка на репозиторий, ссылки — зависимости. Это же — черновик
   каталога сервисов (`services.yaml`) для слоя тегов (см. память `tagging-layer-plan`).

**Прогон на сервере 01.10.2026:** 81 карточка, 43 связи по манифестам, 92 упоминания в конфигах,
без README 11, 43 с. В Continue «в целом отвечает»; разбор ответов — по списку вопросов, следующим шагом.

**Дальше — описания репозиториев от модели: `guides/PLAN-CB-SUMMARY.md`.**

**Потом (пожелание пользователя):** такие же карточки для обычного кода (`CODE_DIR`, code-graph) —
`repo_cards.py` от релиза не зависит, нужен только граф: у code-graph он в `graph.json`, не в SQLite.

## Открытые вопросы

- 51 млн связей на 2.25 млн узлов (~23 на узел, обычно 2–5), больше всего — `main`, почти всё
  EXTRACTED. Какие `relation` и есть ли повторы — раздел 4 `./check-cb-graph.sh`. Если это
  `contains`/`imports` или повторы — фильтровать при загрузке (`graph_store.py`).
- `check-cb.py` раздел 4: «в графе обычного кода репозитории релиза alert-manager, vault-manager».
  **Закрыт 01.10.2026:** ложное срабатывание — одноимённые папки внутри обычных репозиториев
  (подтвердил пользователь). Относительный путь, для которого такая папка есть в CODE_DIR, не считается.
- Почему граф `main` 1.3 ГБ: монорепозиторий или остался мусор (`graphify` по
  `source_file` — где больше всего узлов).
- В `code_survey` добавить оценку суммарного размера графов: больше ~1 ГБ — сразу SQLite.
