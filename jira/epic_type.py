"""Как в этой Jira называется тип «эпик» — значение для JIRA_EPIC_TYPE.

Типов задач в Jira бывают десятки, по списку эпик не угадать. Поэтому
смотрим на сами задачи: у эпика заполнено поле «Epic Name»
(gh-epic-label), обычная задача ссылается на эпик полем «Epic Link»
(gh-epic-link). Поля ищутся по типу, а не по подписи, — переименованные
и переведённые тоже находятся. Только GET, токен из .env не печатается.

Запуск на сервере (работает и на старом образе, без пересборки):
    docker compose exec -T kb python - < jira/epic_type.py
"""

import collections
import os
import sys

sys.path.insert(0, "jira")
from sync import Client  # noqa: E402


def main() -> None:
    e = os.environ.get
    c = Client(
        e("JIRA_URL", ""),
        token=e("JIRA_TOKEN", ""),
        user=e("JIRA_USER", ""),
        password=e("JIRA_PASSWORD", ""),
    )
    fields = c.get("/rest/api/2/field")

    def find(suffix: str) -> list[str]:
        return [
            f["id"]
            for f in fields
            if (f.get("schema") or {}).get("custom", "").endswith(suffix)
        ]

    name_f, link_f = find("gh-epic-label"), find("gh-epic-link")
    print("Поле Epic Name:", name_f, " поле Epic Link:", link_f)
    if not name_f and not link_f:
        print("Полей эпика нет — эпики в этой Jira устроены иначе, прислать вывод")
        return

    found: collections.Counter = collections.Counter()
    projects = e("JIRA_PROJECTS", "").strip()
    scope = f" AND project in ({projects})" if projects else ""

    if name_f:
        cf = name_f[0].split("_")[1]
        r = c.get(
            "/rest/api/2/search",
            jql=f"cf[{cf}] is not EMPTY{scope}",
            maxResults=100,
            fields="issuetype",
        )
        types = collections.Counter(
            i["fields"]["issuetype"]["name"] for i in r["issues"]
        )
        found.update(types)
        print("1) Тип задач с Epic Name:", types.most_common())

    if link_f:
        cf = link_f[0].split("_")[1]
        r = c.get(
            "/rest/api/2/search",
            jql=f"cf[{cf}] is not EMPTY{scope}",
            maxResults=50,
            fields=link_f[0],
        )
        keys = list(
            {i["fields"][link_f[0]] for i in r["issues"] if i["fields"].get(link_f[0])}
        )[:15]
        types = collections.Counter(
            c.get(f"/rest/api/2/issue/{k}", fields="issuetype")["fields"]["issuetype"]["name"]
            for k in keys
        )
        found.update(types)
        print("2) Тип задач, на которые ссылается Epic Link:", types.most_common())

    print()
    if not found:
        print("ИТОГ: эпиков в проектах JIRA_PROJECTS не нашлось — прислать вывод")
    elif len(found) > 1:
        print("ИТОГ: несколько типов", found.most_common(), "— прислать вывод")
    else:
        name = next(iter(found))
        if name == "Epic":
            print("ИТОГ: тип эпика — Epic, как по умолчанию. В .env ничего не писать")
        else:
            print(f"ИТОГ: тип эпика — {name}. Дописать в .env: JIRA_EPIC_TYPE={name}")


if __name__ == "__main__":
    main()
