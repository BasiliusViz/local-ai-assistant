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

## 2а. Как в вашей Jira называется тип эпика

Jira сама скажет: у эпика заполнено поле «Epic Name», у обычной задачи —
«Epic Link». Команда смотрит, какой тип у таких задач в ваших проектах
(только GET, токен из `.env` на экран не выводится, работает и на старом образе).
Блок копировать целиком, до `EOF` включительно:

```bash
docker compose exec -T kb python - <<'EOF'
import os, sys, collections
sys.path.insert(0, "jira")
from sync import Client
e = os.environ.get
c = Client(e("JIRA_URL"), token=e("JIRA_TOKEN", ""), user=e("JIRA_USER", ""), password=e("JIRA_PASSWORD", ""))
fields = c.get("/rest/api/2/field")
def find(suffix):
    return [f["id"] for f in fields if (f.get("schema") or {}).get("custom", "").endswith(suffix)]
name_f, link_f = find("gh-epic-label"), find("gh-epic-link")
print("Поле Epic Name:", name_f, " поле Epic Link:", link_f)
pr = e("JIRA_PROJECTS", "").strip()
scope = f" AND project in ({pr})" if pr else ""
if name_f:
    r = c.get("/rest/api/2/search", jql=f"cf[{name_f[0].split('_')[1]}] is not EMPTY{scope}", maxResults=100, fields="issuetype")
    print("1) Тип задач с Epic Name:", collections.Counter(i["fields"]["issuetype"]["name"] for i in r["issues"]).most_common())
if link_f:
    r = c.get("/rest/api/2/search", jql=f"cf[{link_f[0].split('_')[1]}] is not EMPTY{scope}", maxResults=50, fields=link_f[0])
    keys = list({i["fields"][link_f[0]] for i in r["issues"] if i["fields"].get(link_f[0])})[:15]
    print("2) Тип задач, на которые ссылается Epic Link:", collections.Counter(c.get(f"/rest/api/2/issue/{k}", fields="issuetype")["fields"]["issuetype"]["name"] for k in keys).most_common())
EOF
```

Ждём, например: `1) Тип задач с Epic Name: [('Эпик', 100)]` — это имя и есть.
Если там `Epic` — в `.env` ничего не писать. Другое имя — дописать в `.env`
ровно как в выводе:

```
JIRA_EPIC_TYPE=Эпик
```

## 3. Пересобрать контейнеры

```bash
docker compose up -d --build --force-recreate kb dojo cb-graph && docker compose ps kb dojo cb-graph
```

Ждём: все три `Up`, созданы только что.

## 3а. Если сборка упала: «can't add file ... to tar: io: read/write on closed pipe»

Обычно причина — compose собирает `kb` и `dojo` (и `ollama-gate`) одновременно
из одной папки. Сначала просто собрать один `kb`, без параллели:

```bash
COMPOSE_BAKE=false docker compose build --progress=plain kb 2>&1 | tail -30
```

Прошло — сразу к блоку «остальные и запуск» ниже. Упало — прислать вывод и
выполнить диагностику. Место (не должно быть 100%), чужие файлы в папке
проекта (чего нет в git), файлы без прав на чтение:

```bash
df -h . /var/lib/docker; git status --short | head -20; du -sh $(git status --short --ignored | awk '{print $2}' | grep -vE '^(qdrant|docs|tei|cb|cb-excluded|code|data|corpus|win_old|guides|\.git)/') 2>/dev/null | sort -h | tail -10; find . -path ./qdrant -prune -o -path ./docs -prune -o -path ./cb -prune -o -path ./code -prune -o -path ./tei -prune -o ! -readable -print 2>/dev/null | head -20
```

Если последняя команда вывела файлы — вернуть права (только на код проекта):

```bash
sudo chown -R $(id -u):$(id -g) kb jira confluence tools && chmod -R a+rX kb jira confluence tools
```

Очистить кэш сборщика и собрать заново:

```bash
docker builder prune -f && COMPOSE_BAKE=false docker compose build --progress=plain kb 2>&1 | tail -30
```

Если прошло — остальные и запуск:

```bash
COMPOSE_BAKE=false docker compose build dojo && COMPOSE_BAKE=false docker compose build cb-graph && docker compose up -d --force-recreate kb dojo cb-graph && docker compose ps kb dojo cb-graph
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
