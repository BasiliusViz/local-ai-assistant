# Команды для сервера

Текущий шаг: проверка jira_query и выгрузки эпиков (20.10.2026).
Всё выполнять на сервере, в папке проекта. Блок — копировать целиком.

## 1. Подтянуть код

```bash
git pull && git log --oneline -1
```

Ждём: `COMMANDS.md: ...` (или новее), в `git log` ниже него — `97e5490 jira/sync.py: все эпики...`

## 2. Включить живую Jira

Открыть `.env` и дописать строку (если тип эпика у вас не `Epic` — ещё `JIRA_EPIC_TYPE=...`):

```bash
nano .env
```

```
JIRA_LIVE=1
```

## 3. Пересобрать контейнеры

```bash
docker compose up -d --build --force-recreate kb dojo cb-graph && docker compose ps kb dojo cb-graph
```

Ждём: все три `Up`, созданы только что.

## 3а. Если сборка упала: «can't add file ... to tar: io: read/write on closed pipe»

Права и место на диске (у файла должно быть `rw-r--r--`, свободно — не 100%):

```bash
ls -l kb/jira_retriever.py && df -h . && find kb jira -type f ! -perm -o=r
```

Если `find` вывел файлы — вернуть права на чтение:

```bash
sudo chmod -R a+rX kb jira && sudo chown -R $(id -u):$(id -g) kb jira
```

Собирать по одному образу, без параллели (настоящая ошибка будет видна):

```bash
docker compose build --progress=plain kb 2>&1 | tail -30
```

Если прошло — остальные и запуск:

```bash
docker compose build dojo && docker compose build cb-graph && docker compose up -d --force-recreate kb dojo cb-graph && docker compose ps kb dojo cb-graph
```

## 4. Живая Jira: счёт открытых задач

```bash
docker compose exec kb python -c "from kb import jira_live as j; print(j.query('statusCategory != Done', count_only=True))"
```

Ждём: число, не `error`.

## 5. Живая Jira: выход из рамок проектов

```bash
docker compose exec kb python -c "from kb import jira_live as j; print(j.build_query('x = 1) OR (project = HR'))"
```

Ждём: `JiraLiveError: В JQL лишняя закрывающая скобка`.

## 6. Эпики: сколько найдено

```bash
docker compose exec kb python jira/sync.py --check
```

Ждём: у каждого проекта `эпиков: N`. `ошибка` или `0` — прислать вывод.

```bash
docker compose exec kb python jira/sync.py --dry-run | grep -E "^Эпик|эпиков отдельным"
```

## 7. Эпики: выгрузить и проиндексировать

```bash
docker compose exec kb python jira/sync.py && docker compose exec kb python -m kb.jira_index /docs/jira
```

```bash
docker compose exec kb python jira/sync.py --prune --dry-run
```

Ждём: `вне охвата: 0`.

---

Вывод каждого шага — присылать в чат.
