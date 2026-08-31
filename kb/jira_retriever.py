"""Поиск по задачам Jira в коллекции knowledge.

Устроен иначе, чем kb/retriever.py, и это главное здесь решение.

Вопросы к задачам почти всегда начинаются с ФИЛЬТРА, а не со смысла: «что на
Иванове», «что открыто в DEVSEC», «что горит по проекту». Исполнитель и статус
в тексте задачи не написаны — они лежат в полях, и векторный поиск про них
ничего не знает. Поэтому:

  - если в вопросе есть человек, проект или статус — сначала фильтр, и только
    внутри отобранного работает смысловой поиск;
  - если смысловой части в вопросе нет вовсе («какие задачи на Иванове»), то
    вектор не считаем вообще: отдаём последние по дате изменения. Это не поиск,
    а выборка, и притворяться поиском ей не надо;
  - порог MIN_SCORE применяем ТОЛЬКО когда фильтров нет. Внутри «задач
    Иванова» косинусы низкие просто потому, что выбор мал, и порог, честный
    для всей базы, выкосил бы верные ответы.

Имена людей приходится сопоставлять руками: в вопросе будет «Иванов», а в базе
«Иванов Иван Петрович» или логин i.ivanov. Неоднозначность не разрешаем
угадыванием — возвращаем список совпадений и просим уточнить.
"""

import logging
import re
from dataclasses import dataclass

from qdrant_client import models

from kb import config
from kb.embedder import embed_batch
from kb.retriever import client

log = logging.getLogger(__name__)

SOURCE = "jira"

# Слова, которыми спрашивают про незакрытое и закрытое, когда точное название
# статуса неизвестно. Категория статуса от названий колонок не зависит
OPEN_WORDS = ("откр", "не закр", "незакр", "актуальн", "в работе", "текущ")
DONE_WORDS = ("закр", "готов", "заверш", "сделан", "выполнен")
NOBODY_WORDS = ("не назначен", "никто", "без исполнителя", "ничей", "свободн")


class JiraSearchError(RuntimeError):
    """Ошибка, которую можно показать модели как есть."""


@dataclass
class JiraResult:
    """Найденное вместе с тем, ЧТО именно было применено.

    Разбор типовых отказов RAG сходится в одном: больше всего промахов
    случается не в поиске, а до него — фильтр молча выбрасывает нужное, и
    отличить «в базе нет» от «спросили не то» уже невозможно. Поэтому
    применённые условия возвращаются наружу вместе с результатом.
    """

    issues: list["Issue"]
    applied: dict


@dataclass
class Issue:
    key: str
    summary: str
    status: str
    issue_type: str
    priority: str
    assignee: str
    reporter: str
    url: str
    updated: str
    snippet: str = ""
    found_in: str = ""
    score: float = 0.0
    chunk_idx: int = 0
    # Показать текст даже без смыслового поиска. Нужно при запросе конкретной
    # задачи по номеру: там текст и есть ответ, а не список из одной строки
    show_text: bool = False

    def as_dict(self, detailed: bool = False) -> dict:
        out = {
            "key": self.key,
            "summary": self.summary,
            "status": self.status,
            "assignee": self.assignee or "не назначен",
            "updated": self.updated[:10],
            "url": self.url,
        }
        if detailed:
            out.update(
                {
                    "type": self.issue_type,
                    "priority": self.priority,
                    "reporter": self.reporter,
                    "found_in": self.found_in,
                    "text": self.snippet,
                    "score": round(self.score, 4),
                }
            )
        elif (self.score > 0 or self.show_text) and self.snippet:
            # Цитату кладём, только когда задача НАЙДЕНА по смыслу: там она и
            # есть ответ. В простой выборке («что на Иванове») это была бы
            # карточка задачи, уже разобранная по полям выше — десять таких
            # повторов на ровном месте забивают контекст модели
            out["text"] = self.snippet
        return out


def _jira_only() -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE))
        ]
    )


def available() -> bool:
    """Есть ли в базе задачи. На свежем развёртывании их ещё нет."""
    try:
        if not client().collection_exists(config.COLLECTION):
            return False
        got = client().count(
            collection_name=config.COLLECTION, count_filter=_jira_only(), exact=False
        )
        return got.count > 0
    except Exception as e:
        log.debug("проверка наличия задач не удалась: %s", e)
        return False


def values(field: str, limit: int = 300) -> list[str]:
    """Какие значения поля реально есть среди задач.

    Нужно и для сопоставления имён, и для внятных ошибок: «такого исполнителя
    в базе нет, есть вот эти» гораздо полезнее пустой выдачи.
    """
    try:
        res = client().facet(
            collection_name=config.COLLECTION,
            key=field,
            facet_filter=_jira_only(),
            limit=limit,
        )
        return sorted({str(h.value) for h in res.hits if str(h.value).strip()})
    except Exception as e:  # фасеты есть не во всех версиях Qdrant
        log.debug("facet по %s не сработал: %s", field, e)
        return []


def _norm(text: str) -> str:
    return text.strip().casefold().replace("ё", "е")


def resolve_person(query: str, role: str = "assignee") -> list[str]:
    """Как человека назвали в вопросе -> как он записан в Jira.

    Совпадение ищем по возрастанию строгости: точное, потом вхождение, потом
    по началу слов («иванов и» -> «Иванов Иван»). Возвращаем ВСЕ совпадения:
    однофамильцы — обычное дело, и выбирать за пользователя нельзя.
    """
    wanted = _norm(query)
    if not wanted:
        return []

    names = values(role)
    logins = values(f"{role}_login")

    # Логин совпал — дальше можно не искать, это однозначно
    for login in logins:
        if _norm(login) == wanted:
            return _names_by_login(login, role)

    exact = [n for n in names if _norm(n) == wanted]
    if exact:
        return exact

    partial = [n for n in names if wanted in _norm(n)]
    if partial:
        return partial

    # По началу слов: каждое слово из вопроса должно начинать какое-нибудь
    # слово имени. Так «иванов и» находит «Иванов Иван», но не «Иванова Ольга»
    parts = wanted.split()
    starts = []
    for name in names:
        tokens = _norm(name).split()
        if all(any(t.startswith(p) for t in tokens) for p in parts):
            starts.append(name)
    return starts


def _names_by_login(login: str, role: str) -> list[str]:
    """Отображаемое имя владельца логина: фильтруем именно по имени."""
    try:
        points, _ = client().scroll(
            collection_name=config.COLLECTION,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source", match=models.MatchValue(value=SOURCE)
                    ),
                    models.FieldCondition(
                        key=f"{role}_login", match=models.MatchValue(value=login)
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if points:
            name = (points[0].payload or {}).get(role, "")
            return [name] if name else []
    except Exception as e:
        log.debug("не удалось развернуть логин %s: %s", login, e)
    return []


def _status_conditions(status: str) -> list[models.FieldCondition]:
    """Статус из вопроса -> условие фильтра.

    Сначала пробуем точное название статуса, как оно заведено в проекте. Если
    такого нет, разбираем обиходные слова («открытые», «закрытые») в категорию
    статуса: названия колонок в каждом проекте свои, а категорий всего три.
    """
    wanted = _norm(status)
    known = values("status")

    for name in known:
        if _norm(name) == wanted:
            return [
                models.FieldCondition(key="status", match=models.MatchValue(value=name))
            ]

    hit = [n for n in known if wanted in _norm(n)]
    if hit:
        return [
            models.FieldCondition(key="status", match=models.MatchAny(any=hit))
        ]

    if any(w in wanted for w in DONE_WORDS):
        return [
            models.FieldCondition(
                key="status_category", match=models.MatchValue(value="done")
            )
        ]
    if any(w in wanted for w in OPEN_WORDS):
        return [
            models.FieldCondition(
                key="status_category", match=models.MatchAny(any=["new", "indeterminate"])
            )
        ]

    raise JiraSearchError(
        f"Статуса «{status}» среди задач нет. "
        f"Есть такие: {', '.join(known) if known else 'база пуста'}. "
        "Можно и обобщённо: «открытые» или «закрытые»."
    )


def _dedupe(points, limit: int) -> list[Issue]:
    """Чанки -> задачи.

    Одна задача даёт несколько точек (описание, комментарии), и без склейки
    выдача из десяти строк оказывается тремя задачами. Оставляем лучший чанк
    как цитату: обычно именно он и есть ответ.
    """
    seen: dict[str, Issue] = {}
    for point in points:
        pl = point.payload or {}
        key = pl.get("issue_key", "")
        if not key:
            continue
        score = float(getattr(point, "score", 0.0) or 0.0)
        idx = pl.get("chunk_idx", 0)
        if key in seen:
            best = seen[key]
            # При равных оценках (а в выборке они все нулевые) держим первый
            # чанк задачи: это карточка с описанием, а не случайный
            # комментарий из середины обсуждения
            if best.score > score or (best.score == score and best.chunk_idx <= idx):
                continue
        seen[key] = Issue(
            key=key,
            summary=pl.get("title", "").split(": ", 1)[-1],
            status=pl.get("status", ""),
            issue_type=pl.get("issue_type", ""),
            priority=pl.get("priority", ""),
            assignee=pl.get("assignee", ""),
            reporter=pl.get("reporter", ""),
            url=pl.get("url", ""),
            updated=pl.get("updated_at", ""),
            snippet=pl.get("text", ""),
            found_in="комментарий"
            if pl.get("chunk_kind") == "comment"
            else "описание",
            score=score,
            chunk_idx=idx,
        )
    return list(seen.values())[:limit]


ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")


def get_issue(key: str) -> JiraResult:
    """Одна задача по номеру.

    Отдельный путь, а не частный случай поиска: «что там с DEVSEC-412» — это
    точное обращение, и векторный поиск по такому запросу работает отвратительно
    (номер задачи почти не несёт смысла для эмбеддера). Здесь же нужен ровно
    один документ, и он достаётся фильтром по индексированному полю.
    """
    wanted = key.strip().upper()
    applied = {"issue_key": wanted}

    points, _ = client().scroll(
        collection_name=config.COLLECTION,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="source", match=models.MatchValue(value=SOURCE)
                ),
                models.FieldCondition(
                    key="issue_key", match=models.MatchValue(value=wanted)
                ),
            ]
        ),
        limit=200,
        with_payload=True,
    )

    if not points:
        raise JiraSearchError(
            f"Задачи {wanted} в индексе нет. Возможные причины: она вне "
            "выгружаемых проектов, вне окна по времени (JIRA_SINCE) или "
            "принадлежит не тому, по кому идёт отбор команды. "
            "Проверить можно только в самой Jira."
        )

    issues = _dedupe(points, 1)
    for issue in issues:
        issue.show_text = True
    return JiraResult(issues=issues, applied=applied)


def search_issues(
    query: str | None = None,
    person: str | None = None,
    role: str = "assignee",
    project: str | None = None,
    status: str | None = None,
    issue_key: str | None = None,
    top_k: int = 10,
) -> JiraResult:
    """Задачи под условия. Хотя бы одно условие должно быть задано."""
    if role not in ("assignee", "reporter"):
        raise JiraSearchError("role бывает только assignee или reporter.")

    # Номер задачи перебивает всё остальное: спрашивают конкретную задачу,
    # а не «задачи Иванова, среди которых есть эта»
    if issue_key:
        return get_issue(issue_key)

    # Номер, положенный в query по ошибке, ловим тут же: модель нередко суёт
    # «DEVSEC-412» в поисковый запрос, и векторный поиск его не находит
    if query and ISSUE_KEY.match(query.strip()):
        return get_issue(query)

    applied: dict = {}
    must: list[models.FieldCondition] = [
        models.FieldCondition(key="source", match=models.MatchValue(value=SOURCE))
    ]

    if person:
        if any(w in _norm(person) for w in NOBODY_WORDS):
            must.append(
                models.FieldCondition(key="assignee", match=models.MatchValue(value=""))
            )
            applied["assignee"] = "не назначен"
        else:
            matched = resolve_person(person, role)
            if not matched:
                known = values(role)[:20]
                raise JiraSearchError(
                    f"Человека «{person}» среди задач нет. "
                    + (
                        f"В базе есть: {', '.join(known)}."
                        if known
                        else "Задачи ещё не проиндексированы."
                    )
                )
            if len(matched) > 1:
                raise JiraSearchError(
                    f"Под «{person}» подходит несколько человек: "
                    f"{', '.join(matched)}. Уточни, кто именно нужен."
                )
            must.append(
                models.FieldCondition(
                    key=role, match=models.MatchValue(value=matched[0])
                )
            )
            # Возвращаем РАЗРЕШЁННОЕ имя, а не то, что спросили: «Иванов» и
            # «Иванов Иван Петрович» — разные вещи, и подмену надо видеть
            applied[role] = matched[0]

    if project:
        known = values("project")
        wanted = project.strip().upper()
        if known and wanted not in known:
            raise JiraSearchError(
                f"Проекта «{project}» среди задач нет. Есть: {', '.join(known)}."
            )
        must.append(
            models.FieldCondition(key="project", match=models.MatchValue(value=wanted))
        )
        applied["project"] = wanted

    if status:
        conditions = _status_conditions(status)
        must.extend(conditions)
        # Показываем, во что превратился статус: «открытые» могли стать
        # категорией, а могли — списком конкретных колонок проекта
        applied["status"] = "; ".join(
            f"{c.key}={getattr(c.match, 'value', None) or getattr(c.match, 'any', '')}"
            for c in conditions
        )

    filtered = len(must) > 1  # что-то кроме обязательного source=jira
    if not filtered and not query:
        raise JiraSearchError(
            "Нужно хотя бы одно условие: человек, проект, статус или вопрос."
        )

    flt = models.Filter(must=must)

    # Смысловой части в вопросе нет — значит это выборка, а не поиск.
    # Отдаём свежие сверху: спрашивая «что на Иванове», хотят увидеть то,
    # чем он занят сейчас, а не то, что случайно ближе по вектору
    if not query or not query.strip():
        try:
            points, _ = client().scroll(
                collection_name=config.COLLECTION,
                scroll_filter=flt,
                limit=max(top_k * 6, 60),
                with_payload=True,
                order_by=models.OrderBy(key="updated_at", direction="desc"),
            )
        except Exception as e:
            # order_by требует индекса по полю; на коллекции, созданной
            # старой версией индексатора, его может не быть
            log.debug("scroll с сортировкой не удался: %s", e)
            points, _ = client().scroll(
                collection_name=config.COLLECTION,
                scroll_filter=flt,
                limit=max(top_k * 6, 60),
                with_payload=True,
            )
        applied["sort"] = "по дате изменения, свежие сверху"
        return JiraResult(issues=_dedupe(points, top_k), applied=applied)

    applied["query"] = query
    vector = embed_batch([query])[0]
    found = client().query_points(
        collection_name=config.COLLECTION,
        query=vector,
        using=config.DENSE_VECTOR,
        query_filter=flt,
        limit=max(top_k * 4, 20),
        with_payload=True,
    )
    points = list(found.points)

    # Порог — только для поиска по всей Jira без фильтров. Внутри выборки
    # одного человека или проекта косинусы низкие просто потому, что выбирать
    # не из чего, и общий порог выбросил бы верные ответы
    if not filtered:
        points = [p for p in points if p.score >= config.MIN_SCORE]
        applied["threshold"] = config.MIN_SCORE

    return JiraResult(issues=_dedupe(points, top_k), applied=applied)
