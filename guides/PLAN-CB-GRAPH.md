# План: граф релиза CB18.5 в SQLite + карточки репозиториев

Продолжение `guides/PLAN-CB.md`. Записано 30.09.2026, делать в новой сессии.

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

1. Карточка на репозиторий: README (первые абзацы), языки и объём (из `code_survey`),
   главные модули и «центральные» функции (из SQLite), зависимости от других репозиториев
   (`go.mod`, `pom.xml`, `package.json`, импорты). По желанию — 2–3 фразы от локальной
   модели (через `/v1`, `reasoning_effort: "none"`).
2. Инструмент `cb_repos(query)` — какие репозитории про это; карточки — тот же `graph.sqlite`
   или маленькая коллекция Qdrant.
3. Выгрузка в Obsidian: заметка на репозиторий, ссылки — зависимости. Это же — черновик
   каталога сервисов (`services.yaml`) для слоя тегов (см. память `tagging-layer-plan`).

## Открытые вопросы

- Почему граф `main` 1.3 ГБ: монорепозиторий или остался мусор (`graphify` по
  `source_file` — где больше всего узлов).
- В `code_survey` добавить оценку суммарного размера графов: больше ~1 ГБ — сразу SQLite.
