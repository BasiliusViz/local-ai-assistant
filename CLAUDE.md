# LOCAL-AI — ИИ-ассистент в закрытом контуре

Ассистент для разработчиков, DevOps и DevSecOps: отвечает по внутренней базе
знаний (Confluence, Jira, DefectDojo, код) и ничего не отправляет наружу.
Клиент — Continue в VS Code, модель — Ollama на отдельной машине.

Репозиторий: https://github.com/BasiliusViz/local-ai-assistant (приватный),
ветка `main`. Прод — **Linux-сервер**. Windows-развёртывание сервера больше не
поддерживается, его скрипты лежат в `win_old/`.

**Почему сделано так, а не иначе** (замеры, грабли, отклонённые варианты) —
в `guides/HISTORY.md`. Этот файл — только карта.

**Это не APPSECPROJECT** (`~/Desktop/APPSECPROJECT` — RAG по CVE/CWE, свой
стек). Общего кода нет и быть не должно.

## Как это устроено

```
 VS Code + Continue ──(генерация, напрямую)──> Ollama (другая машина, OLLAMA_URL)
        │                                        ▲
        │ MCP                                    │ эмбеддинги, переформулировка
        ▼                                        │
  kb :8010 ─────────┬────────────────────────────┘
  code-graph :8011  │
  dojo :8012 ───────┴──> qdrant :6333 (коллекции knowledge, code)
```

| Контейнер | Порт | Что даёт модели |
|---|---|---|
| `qdrant` | 6333 | векторная база: `knowledge` (документы, Jira, Dojo), `code` |
| `kb` | 8010 | MCP: `kb_search` (документы), `code_search` (код), `jira_search` |
| `code-graph` | 8011 | MCP Graphify: граф вызовов кода (кто вызывает, что сломается) |
| `dojo` | 8012 | MCP: `dojo_findings`, `dojo_engagements`, `dojo_compare`, `dojo_release_notes`. Отдельный порт ради доступа: только AppSec |
| `reranker` | 8081 | выключен, профиль `quality` |

Ollama стек **не разворачивает**. Видеокарта серверу не нужна.

## Конвейер данных: выгрузка -> индекс -> поиск

| Источник | Выгрузка (хост) | Индекс (в `kb`) | Поиск |
|---|---|---|---|
| Документы | кладутся в `DOCS_DIR/<источник>/` | `kb.doc_index /docs/<папка> --source <имя>` | `kb_search` |
| Confluence | `confluence/sync.py` -> `.md` | `kb.doc_index` | `kb_search` |
| Jira (Server/DC) | `jira/sync.py` -> `.json` | `kb.jira_index /docs/jira` | `jira_search` |
| DefectDojo | нет, API напрямую | `kb.dojo_index` | `dojo_findings` |
| Код | `repos/sync.py` (git по токену) | `kb.code_index` + граф в `code-graph` | `code_search` + граф |

Расписание (systemd-таймеры `install-timers.sh`, либо cron `install-cron.sh`):
Confluence :00, Dojo :15, Jira :30 раз в два часа, чтобы не делить эмбеддер;
Jira `--prune` ночью; код раз в сутки в 2:00 (`repos/cron.sh`).

Всё, кроме кода, индексируется инкрементально (хеш файла/записи в
`.index_state.*.json`). Код переиндексируется целиком.

## Карта репозитория

```
docker-compose.yml, Dockerfile   стек; образ kb общий для kb и dojo
.env.example                     ВСЕ настройки, с объяснениями
deploy.sh / healthcheck.sh       развёртывание с нуля / проверка с живым поиском
update-code.sh                   граф + векторы кода одной командой
install-timers.sh, install-cron.sh   расписание обновлений
selftest.py                      проверка модели, Qdrant и всех MCP по HTTP
ollama_proxy.py                  localhost-прокси, подставляет ключ шлюза сторонним тулам
check-nexus.sh                   разведка зеркала Nexus перед сборкой образов
setup-continue.{sh,ps1}          настройка Continue на машине ПОЛЬЗОВАТЕЛЯ (ps1 нужен:
                                 пользователи на Windows)
continue-config.example.yaml     конфиг Continue, из него собирает setup-continue
continue-rules.yaml              правила для модели (когда звать какой инструмент)

kb/          MCP-серверы и индексаторы (Python)
  server.py            :8010 — kb_search, code_search, jira_search
  dojo_server.py       :8012 — dojo_findings (индекс), dojo_engagements, dojo_compare,
                       dojo_release_notes (живой API)
  config.py            все пороги и переменные
  embedder.py          эмбеддинги через /v1 или native (OLLAMA_API)
  retriever.py         поиск по документам; expander.py — переформулировка запроса
  doc_index.py         документы/Confluence;  jira_index.py; dojo_index.py; code_index.py
  code_chunks.py       нарезка кода tree-sitter (все языки), Python через ast
  *_retriever.py       поиск по своему источнику
  dojo.py, report.py   клиент DefectDojo; HTML-отчёт по уязвимостям
  dojo_compare.py      ветки (engagement): список, одна, сравнение двух — живым API
  release_notes.py     release notes по устранённым уязвимостям (сравнение engagement'ов)
  reranker.py          реранкер (выключен)
  dojo_tool_eval.py    замер: выбирает ли модель нужный инструмент Dojo и поля
  test_*.py            тесты; eval.py, smoke_client.py, tool_call_test.py,
                       auth_probe.py — ручные проверки, прод их не вызывает
confluence/  sync.py, конвертер storage->md, заглушка сервера, тесты, cron.sh
jira/        sync.py, cron.sh, README
dojo/        cron.sh
repos/       sync.py (клон Bitbucket/GitFlic по токену), list.example.txt, тесты
code/        образ code-graph: Graphify + jenkins_graph.py (шаги Jenkins в графе)
testdocs/, corpus/   стенд замеров качества поиска (не прод)
guides/      DEPLOY (развёртывание), USER-GUIDE (для пользователей), HAND-TEST
             (ручная проверка), OASIS-TEST, HANDOFF, HISTORY, presentation/
win_old/     бывший Windows-стенд сервера, не поддерживается
```

У `confluence/`, `jira/`, `repos/`, `code/` свои README: они написаны для
запуска в контуре без меня.

## Правила, которые нельзя нарушать

- **После правки `kb/` — `docker compose up -d --build kb dojo`.** Код внутри
  образа, `git pull` без `--build` ничего не меняет
- **Все инструменты только на чтение.** Модель не пишет ни в базу, ни в
  Jira/Dojo: галлюцинация, закрывшая уязвимость, — инцидент
- **Вызов инструментов по слову:** `jira_search` — «jira», `dojo_findings` —
  «dojo», `kb_search`/`code_search` — по просьбе («поищи в базе», «посмотри в
  коде», «mcp»). Надёжно — только политикой Continue «Ask first»
- **У источника есть свой инструмент — из общего `kb_search` он исключён**
  (`OWN_TOOL_SOURCES` в `kb/retriever.py`)
- **Правка нарезки -> увеличить `CHUNKER_VERSION`** (`JIRA_`/`DOJO_`), иначе
  хеши совпадут и старая нарезка останется
- **`MIN_SCORE` применяется, только когда фильтров нет** (Jira, Dojo): внутри
  выборки косинусы низкие закономерно
- Новые вызовы модели — через OpenAI-совместимый `/v1`, служебным —
  `reasoning_effort: "none"` (секунда вместо минуты)
- Новые переменные окружения прокидывать в `docker-compose.yml`, иначе в
  контейнере их нет
- Выгрузки (`sync.py`) — только стандартная библиотека: в контуре может быть
  нельзя ставить пакеты
- `.sh` — только LF. `.ps1` — UTF-8 с BOM, без `$ErrorActionPreference="Stop"`
- Git Bash портит пути в `docker compose exec` (`/docs/x` -> `C:/Program
  Files/Git/docs/x`): `MSYS_NO_PATHCONV=1`
- Проверять `kb` через `curl :8010/mcp` нельзя — GET висит. Проверка:
  `python selftest.py`

## Доступ к модели (всё в `.env`)

- `OLLAMA_URL` — адрес, `OLLAMA_API_KEY` — ключ шлюза
- Шлюз без Bearer: `OLLAMA_AUTH_HEADER=x-api-key`, `OLLAMA_AUTH_PREFIX=` (пустой)
- Шлюз пускает только `/api/embed`, а `/v1` закрыт: `OLLAMA_API=native`
- Continue ходит в модель напрямую: ключ в его конфиге — в
  `requestOptions.headers`, не в `apiKey`
- Ответ из двух символов = упёрлись в `num_ctx=4096` по умолчанию; лечится
  `defaultCompletionOptions` в конфиге Continue (`selftest.py` это ловит)

## Что проверено, а что нет

Проверено в работе: поиск по документам, Confluence (на Linux, по расписанию;
23.09.2026 — все пространства через `CONFLUENCE_SPACES`, 19 тыс. страниц,
120 тыс. кусков, ссылки на страницы в ответах), DefectDojo (в том числе обзор
по всем продуктам), код и граф, расширение запроса.

Написано, но на живом стенде **не проверялось**: Jira, `repos/sync.py` на
живых серверах, ветки DefectDojo (`dojo_compare.py`, живой API).

**Не сделано:** аутентификации на портах нет (граница доступа — только порт и
брандмауэр), `acl_groups` — заглушка `["all"]`, аудита нет, эталонный набор —
15 вопросов (мал для выводов), индексация кода не инкрементальная, удаление
клонов, убранных из списка.

## Следующий шаг

Пробный `sooperset/mcp-atlassian` рядом с нашим индексом: живые точные запросы
к Jira/Confluence (JQL/CQL) как дополнение к смысловому поиску. Подробности —
`guides/HANDOFF.md`.
