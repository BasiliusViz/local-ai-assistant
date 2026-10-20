# Команды для сервера

Задача: проверить живую Jira (`jira_query`) и выгрузку эпиков. 20.10.2026.
Всё на сервере, в папке проекта. Шаги по порядку, каждый блок копировать целиком.
Вывод каждого шага — присылать в чат.

## Уже сделано

- [x] Тип эпика в Jira — `Epic`, в `.env` для него ничего не пишем
- [ ] `JIRA_LIVE=1` в `.env` — проверьте, что строка есть (шаг 2)

## 1. Подтянуть код

```bash
git pull
```

## 2. Проверить .env

```bash
grep -E "^JIRA_(URL|PROJECTS|LIVE)=" .env; grep -q "^JIRA_TOKEN=." .env && echo "JIRA_TOKEN задан" || echo "JIRA_TOKEN ПУСТОЙ"
```

Ждём: `JIRA_URL` и `JIRA_PROJECTS` заполнены, `JIRA_LIVE=1`, `JIRA_TOKEN задан`.
Нет `JIRA_LIVE=1` — открыть `nano .env`, дописать строку `JIRA_LIVE=1`, сохранить.

## 3. Собрать и запустить контейнеры

Собираем по одному образу — так не бывает ошибки `tar ... closed pipe`.
Сначала `kb`:

```bash
COMPOSE_BAKE=false docker compose build kb
```

Ждём в конце: `Built` или `naming to ... done`. Упало — прислать последние
строки и не идти дальше (разбор — в конце файла).

Затем остальные два и запуск всех трёх:

```bash
COMPOSE_BAKE=false docker compose build dojo && COMPOSE_BAKE=false docker compose build cb-graph && docker compose up -d --force-recreate kb dojo cb-graph && docker compose ps kb dojo cb-graph
```

Ждём: три строки `kb`, `dojo`, `cb-graph`, у всех `Up`.

## 4. Живая Jira: считает задачи

```bash
docker compose exec kb python -c "from kb import jira_live as j; print(j.query('statusCategory != Done', count_only=True))"
```

Ждём: число, а не `error`.

## 5. Живая Jira: не выходит за свои проекты

```bash
docker compose exec kb python -c "from kb import jira_live as j; print(j.build_query('x = 1) OR (project = HR'))"
```

Ждём: ошибку `JiraLiveError: В JQL лишняя закрывающая скобка` — это правильно.

## 6. Эпики: сколько найдено

```bash
docker compose exec kb python jira/sync.py --check
```

Ждём: у каждого проекта `эпиков: N`, где N больше нуля.

## 7. Эпики: выгрузить и проиндексировать

Может идти долго — первый раз берутся все эпики.

```bash
docker compose exec kb python jira/sync.py && docker compose exec kb python -m kb.jira_index /docs/jira
```

Проверка, что ничего лишнего не удалится:

```bash
docker compose exec kb python jira/sync.py --prune --dry-run
```

Ждём: `вне охвата: 0`.

---

## Если шаг 3 упал

Только если сборка `kb` не прошла. Место на диске, лишние файлы в папке
проекта, файлы без прав на чтение:

```bash
df -h . /var/lib/docker; git status --short | head -20; find . -path ./qdrant -prune -o -path ./docs -prune -o -path ./cb -prune -o -path ./code -prune -o -path ./tei -prune -o ! -readable -print 2>/dev/null | head -20
```

Прислать вывод. Если в конце есть файлы — вернуть права и собрать заново:

```bash
sudo chown -R $(id -u):$(id -g) kb jira confluence tools && chmod -R a+rX kb jira confluence tools && docker builder prune -f && COMPOSE_BAKE=false docker compose build kb
```
