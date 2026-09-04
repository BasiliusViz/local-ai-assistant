"""Клиент DefectDojo: продукты и находки.

Находки выгружаются в Qdrant (`kb/dojo_index.py`) и ищутся там же, как
документация, код и задачи, — механика у всех источников одна. Здесь только
HTTP-клиент, которым пользуется индексатор, и проверка связи.

У индекса всегда есть возраст, и для уязвимостей это важнее, чем для
документации: находки меняются каждый день — новый скан, смена статуса,
принятый риск. Поэтому ответ верен на момент последней выгрузки, и инструмент
это проговаривает, как и с задачами Jira. Живой запрос в момент вопроса —
следующий шаг, клиент для него здесь уже есть.

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

import httpx

log = logging.getLogger(__name__)

URL = os.getenv("DOJO_URL", "").strip().rstrip("/")
TOKEN = os.getenv("DOJO_TOKEN", "").strip()
PRODUCTS = [p.strip() for p in os.getenv("DOJO_PRODUCTS", "").split(",") if p.strip()]
VERIFY_TLS = os.getenv("DOJO_VERIFY_TLS", "1").strip() != "0"
# Секунд на запрос. По умолчанию щедро: /products/ у DefectDojo тяжёлый —
# на каждую строку он подтягивает связанные объекты, и на большой инсталляции
# страница отдаётся десятками секунд
TIMEOUT = float(os.getenv("DOJO_TIMEOUT", "90"))
PAGE_SIZE = 100
# Продукты просим мелкими страницами по той же причине: один запрос на 500
# записей укладывается в таймаут далеко не везде
PRODUCT_PAGE = 25

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
    except httpx.TimeoutException as e:
        raise DojoError(
            f"Таймаут {TIMEOUT:.0f} с на запросе {path}.\n"
            "  - список продуктов в DefectDojo тяжёлый сам по себе: он "
            "подтягивает связанные объекты на каждую строку\n"
            "  - если инсталляция большая, поднимите DOJO_TIMEOUT в .env "
            "(секунды) и пересоздайте контейнер\n"
            "  - заполненный DOJO_PRODUCTS ускоряет всё заметно: продукты "
            "запрашиваются поимённо, а не списком целиком\n"
            "  - проверьте вручную, сколько это занимает:\n"
            f'    curl -s -o /dev/null -w "%{{time_total}}\\n" -H '
            f'"Authorization: Token <ключ>" "{URL}/api/v2/products/?limit=25"'
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


def _all_products(client: httpx.Client) -> list[dict]:
    """Весь список продуктов, мелкими страницами.

    Одним запросом на 500 записей это укладывалось в таймаут не везде:
    /products/ у DefectDojo подтягивает связанные объекты на каждую строку,
    и на большой инсталляции страница отдаётся десятками секунд.
    """
    out: list[dict] = []
    offset = 0
    while True:
        data = _get(client, "/products/", limit=PRODUCT_PAGE, offset=offset)
        results = data.get("results", [])
        out.extend(results)
        offset += len(results)
        if not results or offset >= int(data.get("count", 0)):
            return out


def products(client: httpx.Client) -> list[dict]:
    """Продукты, которые нам разрешено показывать.

    Когда DOJO_PRODUCTS задан, спрашиваем их ПОИМЁННО — это несколько крошечных
    запросов вместо обхода всего каталога, и на инсталляции с сотнями продуктов
    разница между «секунда» и «таймаут».

    Если фильтр по имени в этой сборке DefectDojo не сработал (у разных версий
    он ведёт себя по-разному: точное совпадение против вхождения), откатываемся
    на полный обход и отбираем на своей стороне.
    """
    if PRODUCTS:
        picked: dict[int, dict] = {}
        for name in PRODUCTS:
            data = _get(client, "/products/", name=name, limit=PRODUCT_PAGE)
            for item in data.get("results", []):
                # Фильтр мог вернуть лишнее (вхождение вместо совпадения) —
                # оставляем только то, что действительно просили
                if _norm(item.get("name", "")) == _norm(name):
                    picked[item["id"]] = item
        if picked:
            return list(picked.values())
        log.debug("фильтр по имени ничего не дал, читаем каталог целиком")

    found = _all_products(client)
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


def scanner_name(item: dict) -> str:
    """Чем нашли. Ключ зависит от версии DefectDojo, поэтому перебираем.

    В разных сборках это found_by_name, список found_by или имя типа теста —
    гадать бесполезно, дешевле проверить по очереди и не упасть, если нет
    ничего: сканер в находке приятная деталь, а не обязательное поле.
    """
    for key in ("found_by_name", "test_type_name", "scanner"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return ", ".join(str(v) for v in value)
    return ""


def finding_status(item: dict) -> str:
    """Одно слово вместо четырёх флагов DefectDojo.

    Порядок проверок важен: находка может быть одновременно неактивной и
    закрытой, и принятый риск важнее, чем «неактивна», — он объясняет почему.
    """
    if item.get("false_p"):
        return "false_positive"
    if item.get("risk_accepted"):
        return "accepted"
    if item.get("is_mitigated") or item.get("mitigated"):
        return "fixed"
    if item.get("active"):
        return "open"
    return "inactive"


def all_findings(client: httpx.Client, product_id: int):
    """Все находки продукта, постранично. Для индексатора.

    Дубликаты пропускаем: DefectDojo помечает их сам, и в индексе они дали бы
    по нескольку одинаковых ответов на один вопрос.
    """
    offset = 0
    while True:
        data = _get(
            client,
            "/findings/",
            **{
                "test__engagement__product": product_id,
                "duplicate": "false",
                "limit": PAGE_SIZE,
                "offset": offset,
                "ordering": "id",
            },
        )
        results = data.get("results", [])
        for item in results:
            yield item
        offset += len(results)
        if not results or offset >= int(data.get("count", 0)):
            return


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
