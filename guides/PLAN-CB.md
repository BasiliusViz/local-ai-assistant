# План: код релиза CB18.5 отдельно от обычного кода

Тест: если модель хорошо понимает код релиза — оставляем и обобщаем, если нет — убираем.

## Что получится

| | Обычный код | Релиз CB18.5 |
|---|---|---|
| Клоны | `CODE_DIR`, `repos/list.txt` | `CB_DIR`, `repos/cb.txt` |
| Поиск | `code_search`, коллекция `code` | `cb_search`, коллекция `code_cb` (тот же `kb` :8010) |
| Граф | `code-graph` :8011 | `cb-graph` :8013, тот же образ, инструменты `cb_*` |
| Слово в чате | «в коде» | «в релизе», «CB», «18.5» |
| Обновление | вручную `repos/cron.sh` (ночной таймер отключён) | вручную `./update-cb.sh` |

## Что выяснилось при проверке

1. `repos/sync.py` склеивает `CODE_GIT_REPOS` из `.env` и файл списка — в `update-cb.sh`
   явно обнулить `CODE_GIT_REPOS=""`, иначе в релиз попадёт обычный код.
2. `code/sync.sh` клонирует по `CODE_REPOS` — у `cb-graph` она должна быть пустой.
3. Graphify: инструменты `query_graph, get_node, get_neighbors, get_community, god_nodes,
   graph_stats, shortest_path`, префикса имён нет. Два сервера с одинаковыми именами
   клиенты MCP путают -> переименование через прослойку.
4. Образ ставит `graphifyy[mcp]` без версии; в текущем main на GitHub нет `--transport http`,
   который у нас в CMD. Пересборка может сломать граф -> закрепить версию, что стоит сейчас.
5. Правила для модели лежат в ДВУХ местах: `continue-rules.yaml` и внутри
   `continue-config.example.yaml`.
6. Граф целиком грузится в память сервера: релиз в 10 раз больше — следить за RAM `cb-graph`.
7. `install-timers.sh` / `install-cron.sh` при повторном запуске снова включат ночной код.
8. `cb-graph` при старте граф не строит (строит только `update-cb.sh`). Graphify внутри
   работает в фоне: упадёт — прослойка отвечает 502, лечится `docker compose restart cb-graph`.

## Задачи

Сессия 1 — поиск (СДЕЛАНО 27.09.2026, тест `kb/test_code_search.py`; не закоммичено,
на сервере не проверялось):
1. `kb/code_index.py` — параметр `--collection` (по умолчанию `code`).
2. `kb/code_retriever.py` — `available/repos/search` принимают коллекцию.
3. `kb/server.py` — `cb_search` (readOnly), общее тело с `code_search`, описание: только «релиз/CB/18.5».
4. `docker-compose.yml`, `.env.example` — `CB_DIR` в `kb` как `/cb:ro`.
5. `kb/test_code_retriever.py` — поиск идёт в переданную коллекцию; `cb_search` зарегистрирован.

Сессия 2 — граф и обвязка (СДЕЛАНО 27.09.2026; на сервере не проверялось):
6. `code/mcp_prefix.py` — прослойка MCP: берёт инструменты у Graphify, отдаёт с префиксом `cb_`
   и пометкой «РЕЛИЗ CB18.5» в описании, вызовы пересылает обратно. Тест.
7. `docker-compose.yml` — сервис `cb-graph`: образ `code-graph`, `CB_DIR:/data`, `CODE_REPOS=`,
   Graphify внутри на 8011, наружу прослойка на `CB_GRAPH_PORT` (8013).
8. `code/Dockerfile` — `graphifyy[mcp]==0.9.48` (версия с сервера).
9. `update-cb.sh` — sync.py с `CODE_DIR=$CB_DIR CODE_GIT_REPOS="" CODE_GIT_REPOS_FILE=repos/cb.txt`,
   граф в `cb-graph`, `kb.code_index /cb --collection code_cb --recreate`. `repos/cb.example.txt`.
10. `continue-config.example.yaml`, `setup-continue.{sh,ps1}` — сервер `cb-graph`, проверка порта 8013.
11. `continue-rules.yaml` + правила в конфиге — «в релизе» -> `cb_search`/`cb_*`, «в коде» -> обычные.
12. `selftest.py`, `healthcheck.sh` — проверка `cb-graph` и `cb_search`.
13. `CLAUDE.md` (таблица), `guides/HAND-TEST.md` (проверка), `guides/DEPLOY.md` (как отключить ночной код).
14. Ревью `reviewer` по диффу обеих сессий.

## Проверка на сервере

1. `docker compose up -d --build kb dojo cb-graph`
2. `./update-cb.sh`
3. `python3 selftest.py`
4. Continue: «посмотри в релизе, где …», «кто в релизе вызывает …», «посмотри в коде …» —
   ответы не смешиваются.
