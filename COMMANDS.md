# Команды для сервера

Задача: проверить живую Jira (`jira_query`) и выгрузку эпиков. 20.10.2026.
Всё на сервере, в папке проекта. Одна команда — один шаг, строго по порядку.
Не совпало с «Ждём» — остановиться и прислать вывод.

Уже сделано: тип эпика в Jira — `Epic`, в `.env` для него ничего не пишем.

---

### 1

```bash
git pull
```

Ждём: `Fast-forward` или `Already up to date`.

### 2

```bash
grep -E "^JIRA_(URL|PROJECTS|LIVE)=" .env
```

Ждём: три строки, `JIRA_URL` и `JIRA_PROJECTS` заполнены, `JIRA_LIVE=1`.
Нет строки `JIRA_LIVE=1` — шаг 3, иначе сразу шаг 4.

### 3

```bash
echo "JIRA_LIVE=1" >> .env
```

Ничего не выводит — это нормально.

### 4

```bash
grep -q "^JIRA_TOKEN=." .env && echo "JIRA_TOKEN задан" || echo "JIRA_TOKEN ПУСТОЙ"
```

Ждём: `JIRA_TOKEN задан`.

### 5

```bash
COMPOSE_BAKE=false docker compose build kb
```

Ждём в конце: `Built` или `naming to ... done`. Упало — в конец файла, «Если шаг 5 упал».

### 6

```bash
COMPOSE_BAKE=false docker compose build dojo
```

Ждём: то же, `Built`.

### 7

```bash
COMPOSE_BAKE=false docker compose build cb-graph
```

Ждём: то же, `Built`.

### 8

```bash
docker compose up -d --force-recreate kb dojo cb-graph
```

Ждём: `Started` у трёх контейнеров.

### 9

```bash
docker compose ps kb dojo cb-graph
```

Ждём: у всех трёх `Up`.

### 10

```bash
docker compose exec kb python -c "from kb import jira_live as j; print(j.query('statusCategory != Done', count_only=True))"
```

Ждём: число открытых задач, а не `error`.

### 11

```bash
docker compose exec kb python -c "from kb import jira_live as j; print(j.build_query('x = 1) OR (project = HR'))"
```

Ждём: ошибку `JiraLiveError: В JQL лишняя закрывающая скобка` — так и должно быть.

### 12

```bash
docker compose exec kb python jira/sync.py --check
```

Ждём: у каждого проекта `эпиков: N`, N больше нуля.

### 13

```bash
docker compose exec kb python jira/sync.py
```

Может идти долго: первый раз выгружаются все эпики.

### 14

```bash
docker compose exec kb python -m kb.jira_index /docs/jira
```

Ждём: в конце итог индексации без ошибок.

### 15

```bash
docker compose exec kb python jira/sync.py --prune --dry-run
```

Ждём: `вне охвата: 0`.

---

## Если шаг 5 упал

### А

```bash
df -h . /var/lib/docker
```

Ждём: ни у одной строки не `100%`.

### Б

```bash
find kb jira confluence tools ! -readable -print
```

Ждём: пусто. Вывел файлы — шаг В, иначе прислать вывод шага 5.

### В

```bash
sudo chown -R $(id -u):$(id -g) kb jira confluence tools
```

### Г

```bash
docker builder prune -f
```

Затем снова шаг 5.
