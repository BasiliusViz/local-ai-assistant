"""Находки DefectDojo: живой запрос к API, без индексации.

Почему не как Confluence и Jira. Находки меняются каждый день: приходит новый
скан, меняется статус, риск принимают или закрывают. Ответ «критичных нет» по
вчерашнему снимку хуже, чем отсутствие ответа: на него полагаются, а он врёт.
Плюс вопросы к DefectDojo по форме счётные — «сколько открытых высоких по
продукту», — а не смысловые, и вектор для них бесполезен. Поэтому здесь
обычный HTTP-запрос в момент вопроса.

Только чтение. Менять статусы находок из чата нельзя: галлюцинация, попавшая
в тикет, — это неприятно, а галлюцинация, закрывшая уязвимость, — инцидент.

Настройки в .env:
    DOJO_URL        https://defectdojo.company.local
    DOJO_TOKEN      API-ключ (профиль -> API v2 Key)
    DOJO_PRODUCTS   abinf,payments   разрешённые продукты через запятую
    DOJO_VERIFY_TLS 0 — не проверять сертификат (внутренний УЦ)

DOJO_PRODUCTS — граница доступа, а не удобство. Пока на порту 8010 нет
аутентификации, спросить у ассистента может любой, кто до него дотянулся;
список продуктов ограничивает, что он вообще способен рассказать.

Проверка связи:
    docker compose exec kb python -m kb.dojo --check
"""

import logging
import os
import sys
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

URL = os.getenv("DOJO_URL", "").strip().rstrip("/")
TOKEN = os.getenv("DOJO_TOKEN", "").strip()
PRODUCTS = [p.strip() for p in os.getenv("DOJO_PRODUCTS", "").split(",") if p.strip()]
VERIFY_TLS = os.getenv("DOJO_VERIFY_TLS", "1").strip() != "0"
TIMEOUT = 30.0

# Порядок важен: в таком виде выдаём сводку и в таком же считаем «сначала
# худшее». Значения — как их пишет сам DefectDojo
SEVERITIES = ("Critical", "High", "Medium", "Low", "Info")

# Как про уровни спрашивают по-русски. Ключ — то, что модель положит в аргумент
SEVERITY_RU = {
    "критич": "Critical",
    "critical": "Critical",
    "высок": "High",
    "high": "High",
    "средн": "Medium",
    "medium": "Medium",
    "низк": "Low",
    "low": "Low",
    "инфо": "Info",
    "info": "Info",
}

# Состояния находки. active=true и есть «открытые»: то, с чем надо работать
STATUS_FILTERS = {
    "open": {"active": "true", "duplicate": "false"},
    "accepted": {"risk_accepted": "true"},
    "false_positive": {"false_p": "true"},
    "fixed": {"is_mitigated": "true"},
    "all": {"duplicate": "false"},
}


class DojoError(RuntimeError):
    """Ошибка с объяснением, которое можно показать модели как есть."""


@dataclass
class Finding:
    id: int
    title: str
    severity: str
    component: str
    scanner: str
    found_at: str
    url: str

    def as_dict(self) -> dict:
        out = {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "url": self.url,
        }
        if self.component:
            out["component"] = self.component
        if self.scanner:
            out["scanner"] = self.scanner
        if self.found_at:
            out["found"] = self.found_at[:10]
        return out


def configured() -> bool:
    return bool(URL and TOKEN)


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=f"{URL}/api/v2",
        headers={"Authorization": f"Token {TOKEN}", "Accept": "application/json"},
        timeout=TIMEOUT,
        verify=VERIFY_TLS,
    )


def _get(client: httpx.Client, path: str, **params) -> dict:
    try:
        resp = client.get(path, params=params)
    except httpx.ConnectError as e:
        raise DojoError(
            f"Не удалось соединиться с DefectDojo ({URL}): {e}. "
            "Проверьте адрес и доступность с этой машины."
        ) from e
    except httpx.HTTPError as e:
        raise DojoError(f"Запрос к DefectDojo не удался: {e}") from e

    if resp.status_code == 401:
        raise DojoError(
            "401: DefectDojo не принял ключ. Он берётся в профиле "
            "пользователя, раздел API v2 Key, и уходит заголовком "
            "Authorization: Token <ключ> — это не пароль и не JWT."
        )
    if resp.status_code == 403:
        raise DojoError(
            "403: ключ принят, но прав на эти данные нет. Проверьте, что "
            "учётная запись видит нужные продукты в веб-интерфейсе."
        )
    if resp.status_code == 404:
        raise DojoError(
            f"404: пути {path} нет. Обычно это значит, что в DOJO_URL попал "
            "лишний хвост — адрес нужен без /api/v2."
        )
    if resp.status_code >= 400:
        raise DojoError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    return resp.json()


def _norm(text: str) -> str:
    return text.strip().casefold().replace("ё", "е")


def normalize_severity(value: str) -> str:
    """«критичные», «high», «Critical» -> значение, понятное DefectDojo."""
    wanted = _norm(value)
    for prefix, canonical in SEVERITY_RU.items():
        if wanted.startswith(prefix):
            return canonical
    raise DojoError(
        f"Уровень «{value}» непонятен. Бывают: критичный, высокий, средний, "
        "низкий, информационный."
    )


def products(client: httpx.Client) -> list[dict]:
    """Продукты, которые нам разрешено показывать.

    Список фильтруем на своей стороне, а не запросом: у разных версий
    DefectDojo фильтр по имени ведёт себя по-разному (точное совпадение против
    вхождения), а продуктов в любом случае немного.
    """
    data = _get(client, "/products/", limit=500)
    found = data.get("results", [])
    if not PRODUCTS:
        return found
    allowed = {_norm(p) for p in PRODUCTS}
    return [p for p in found if _norm(p.get("name", "")) in allowed]


def resolve_product(client: httpx.Client, name: str) -> dict:
    """Название из вопроса -> продукт DefectDojo.

    Сначала точное совпадение, потом вхождение: спрашивают «abinf», а в
    DefectDojo он может называться «ABInf Backend».
    """
    visible = products(client)
    if not visible:
        raise DojoError(
            "Ни одного доступного продукта. Либо у ключа нет прав, либо в "
            "DOJO_PRODUCTS перечислены названия, которых в DefectDojo нет."
        )

    wanted = _norm(name)
    exact = [p for p in visible if _norm(p.get("name", "")) == wanted]
    if exact:
        return exact[0]

    partial = [p for p in visible if wanted in _norm(p.get("name", ""))]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(p.get("name", "") for p in partial)
        raise DojoError(f"Под «{name}» подходит несколько продуктов: {names}.")

    names = ", ".join(p.get("name", "") for p in visible[:20])
    raise DojoError(f"Продукта «{name}» нет среди доступных. Есть: {names}.")


def _count(client: httpx.Client, product_id: int, status: str, severity: str) -> int:
    """Сколько находок под условие.

    limit=1 и читаем count: DefectDojo отдаёт общее число в каждом ответе, и
    тянуть ради счётчика все находки незачем.
    """
    params = dict(STATUS_FILTERS[status])
    params.update(
        {
            "test__engagement__product": product_id,
            "severity": severity,
            "limit": 1,
        }
    )
    return int(_get(client, "/findings/", **params).get("count", 0))


def summary(client: httpx.Client, product_id: int, status: str) -> dict:
    """Сводка по уровням: то, чем отвечают на «что у нас по продукту»."""
    return {sev: _count(client, product_id, status, sev) for sev in SEVERITIES}


def findings(
    client: httpx.Client,
    product_id: int,
    status: str,
    severity: str | None,
    limit: int,
) -> list[Finding]:
    """Сами находки, сначала самые серьёзные и свежие."""
    params = dict(STATUS_FILTERS[status])
    params.update(
        {
            "test__engagement__product": product_id,
            "limit": limit,
            # Сортировка DefectDojo: минус — по убыванию. Сначала уровень,
            # потом дата, чтобы наверх попадало худшее и недавнее
            "ordering": "-numerical_severity,-date",
        }
    )
    if severity:
        params["severity"] = severity

    data = _get(client, "/findings/", **params)
    out = []
    for item in data.get("results", []):
        out.append(
            Finding(
                id=item.get("id", 0),
                title=(item.get("title") or "").strip(),
                severity=item.get("severity", ""),
                component=" ".join(
                    filter(
                        None,
                        [item.get("component_name") or "", item.get("component_version") or ""],
                    )
                ).strip(),
                scanner=(item.get("found_by_name") or ""),
                found_at=item.get("date") or "",
                url=f"{URL}/finding/{item.get('id')}",
            )
        )
    return out


def report(
    product: str,
    status: str = "open",
    severity: str | None = None,
    limit: int = 10,
) -> dict:
    """Готовый ответ по продукту: сводка плюс сами находки."""
    if not configured():
        raise DojoError(
            "DefectDojo не настроен: нужны DOJO_URL и DOJO_TOKEN в .env, "
            "после правки — docker compose up -d kb"
        )
    if status not in STATUS_FILTERS:
        raise DojoError(
            "status бывает: open (по умолчанию), accepted, false_positive, "
            "fixed, all."
        )

    canonical = normalize_severity(severity) if severity else None

    with _client() as client:
        found = resolve_product(client, product)
        pid = found["id"]
        return {
            "product": found.get("name", ""),
            "product_url": f"{URL}/product/{pid}",
            "status": status,
            "summary": summary(client, pid, status),
            "findings": [f.as_dict() for f in findings(client, pid, status, canonical, limit)],
        }


def main() -> int:
    """Проверка связи: docker compose exec kb python -m kb.dojo --check"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not configured():
        print("Не заданы DOJO_URL и DOJO_TOKEN.")
        print("Пропишите их в .env, затем: docker compose up -d kb")
        return 2

    print(f"DefectDojo: {URL}")
    print(f"Разрешённые продукты: {', '.join(PRODUCTS) if PRODUCTS else 'все видимые'}")

    try:
        with _client() as client:
            visible = products(client)
            print(f"\nДоступно продуктов: {len(visible)}")
            for item in visible[:20]:
                counts = summary(client, item["id"], "open")
                line = "  ".join(f"{sev[:4]}: {counts[sev]:>4}" for sev in SEVERITIES)
                print(f"    {item.get('name', ''):30} {line}")
    except DojoError as e:
        print(f"\n{e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
