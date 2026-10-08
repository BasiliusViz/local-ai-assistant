# Веб-чат Open WebUI

То же, что Continue в VS Code — модель и наши MCP, — но в браузере.
Почему так и какие камни закрытого контура обойдены — `PLAN-OPEN-WEBUI.md`.

Пока один пользователь — админ. Группы и отдельная модель без Dojo — когда
появятся другие пользователи (камни 9 и 13 в плане).

## Как он подключается

```
браузер ──HTTPS :443──> webui-tls (nginx) ──> open-webui ──> ollama-gate:11435 ──(+ x-api-key)──> шлюз Ollama (OLLAMA_URL)
                       │          (только внутри compose, pull/delete/create — 403)
                       │
                       └── MCP по внутренней сети Docker:
                           http://kb:8010/mcp, http://code-graph:8011/mcp,
                           http://cb-graph:8013/mcp, http://dojo:8012/mcp
```

**Почему через прокси, а не ключ в настройках Open WebUI.** В Open WebUI
(v0.11.4 и `main` на 08.10.2026) свои заголовки подключения уходят только в
POST — в генерацию. Список моделей (`GET /api/tags`), `/api/version`,
`/api/ps` и кнопка проверки соединения шлют максимум `Authorization: Bearer`,
без `x-api-key`. Шлюз на них ответит 403, и моделей в списке не будет. Поле
API Key не спасает по той же причине, что `apiKey` в Continue. Поэтому ключ
дописывает `ollama-gate` (`ollama_proxy.py` из образа `kb`), а у самого
Open WebUI ключа нет вовсе. Заодно прокси не пускает `pull`, `push`, `create`,
`copy`, `delete`, `blobs` — кнопки скачать/удалить модель в админке не
сработают, даже если шлюз эти пути не закрыл.

MCP открываются по именам контейнеров, а не по адресу сервера: Open WebUI
работает в той же сети compose. Порты на хосте для этого не нужны.

## 1. Образ

Версия закреплена: `v0.11.4`, полный образ (не `slim`).

Если в контуре есть зеркало ghcr.io в Nexus — в `.env`:

```
WEBUI_IMAGE=nexus.company.local:8082/open-webui/open-webui:v0.11.4
```

Иначе на машине с интернетом:

```bash
docker pull ghcr.io/open-webui/open-webui:v0.11.4
```

```bash
docker save ghcr.io/open-webui/open-webui:v0.11.4 -o open-webui-v0.11.4.tar
```

Файл перенести на сервер, там:

```bash
docker load -i open-webui-v0.11.4.tar
```

## 2. Настройки в `.env`

Раздел «веб-чат Open WebUI» в `.env.example`. Обязательно:

- `WEBUI_SECRET_KEY` — `openssl rand -hex 32` (уже задан — не менять)
- `WEBUI_ADMIN_EMAIL`, `WEBUI_ADMIN_PASSWORD` — админ создаётся при первом
  старте, регистрация сразу закрыта. После первого входа пароль из `.env`
  стереть — он уже в базе
- `WEBUI_ORIGIN` — адрес чата, как в браузере: `https://10.0.0.5`

Адрес и ключ шлюза отдельно не задаются: `ollama-gate` берёт `OLLAMA_URL`
(`/v1` с конца отрезает сам), `OLLAMA_API_KEY`, `OLLAMA_AUTH_HEADER`,
`OLLAMA_AUTH_PREFIX` — те же, что у `kb`. Для шлюза с `x-api-key`:

```
OLLAMA_AUTH_HEADER=x-api-key
OLLAMA_AUTH_PREFIX=
```

Без `OLLAMA_API_KEY` прокси не стартует (ключ не нужен — значит, и прокси
не нужен; тогда в Connections указать адрес Ollama напрямую).

**Переменные применяются только при первом старте.** Потом Open WebUI
хранит настройки в своей базе (том `open-webui`), и правка `.env` их уже не
меняет — менять в админке. Так задумано: иначе перезапуск сбрасывал бы MCP,
настроенные руками.

## 3. Сертификат HTTPS

Порт Open WebUI наружу не открыт: по HTTP пароль и чаты шли бы открытым
текстом. Снаружи — только HTTPS через `webui-tls` (nginx,
`webui/nginx.conf.template`). Самоподписанный сертификат — на сервере, в
папке проекта. `АДРЕС` — IP сервера, по которому открываете чат (и имя, если
есть: `DNS:имя` через запятую):

```bash
mkdir -p data/webui-cert
```

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout data/webui-cert/server.key -out data/webui-cert/server.crt -subj "/CN=local-ai" -addext "subjectAltName=IP:АДРЕС"
```

```bash
chmod 600 data/webui-cert/server.key
```

nginx читает ключ от root при старте, остальным пользователям сервера он не
нужен. Папка `data/` в git не попадает. Браузер один раз предупредит о недоверенном сертификате —
«Дополнительно → перейти». Чтобы не предупреждал, `server.crt` добавить в
доверенные корневые на своём компьютере. Выдаст сертификат внутренний CA —
положить его файлы под теми же именами и `docker compose --profile webui
restart webui-tls`.

## 4. Запуск

```bash
docker compose --profile webui up -d --build ollama-gate open-webui webui-tls
```

```bash
docker logs ollama-gate
```

Должно быть «Пересылаю в http://..., добавляя заголовок x-api-key».

```bash
docker compose exec ollama-gate python -c "import requests; print(requests.get('http://localhost:11435/api/tags').status_code)"
```

Должно быть `200`: шлюз принял ключ.

```bash
docker logs -f open-webui
```

Ждать строку о запуске на порту 8080. Затем открыть `https://АДРЕС-СЕРВЕРА`
и войти почтой и паролем из `WEBUI_ADMIN_EMAIL`/`WEBUI_ADMIN_PASSWORD`. Регистрации нет. Затем стереть пароль из `.env`.

Сразу после входа, до любых настроек, проверить, что наружу ничего не ушло:

```bash
./check-egress.sh --quiet --watch 600
```

## 5. Админка (руками, один раз)

**Admin Panel → Settings → Connections.** Ollama: `http://ollama-gate:11435`
(уже подставлено при первом старте), **поле API Key и Headers пустые** —
ключ дописывает прокси. OpenAI API выключен. «Проверить соединение» —
зелёное.

**Admin Panel → Settings → Integrations → External Tool Servers → + Add
Connection**, четыре раза:

| Type | URL | Auth | Name |
|---|---|---|---|
| MCP (Streamable HTTP) | `http://kb:8010/mcp` | None | kb |
| MCP (Streamable HTTP) | `http://code-graph:8011/mcp` | None | code-graph |
| MCP (Streamable HTTP) | `http://cb-graph:8013/mcp` | None | cb-graph |
| MCP (Streamable HTTP) | `http://dojo:8012/mcp` | None | dojo |

После сохранения у каждого должен появиться список инструментов.

**Dojo — только AppSec.** Пока пользователь один, не горит. Перед тем как
пускать других: у подключения `dojo` в Access Control — Private или группа
AppSec (у Continue граница — отдельный порт 8012, а Open WebUI ходит к `dojo`
изнутри Docker, мимо неё).

**Admin Panel → Settings → Audio:** STT и TTS — не использовать (Whisper
вшит в образ, но не нужен).

## 6. Модель «LOCAL-AI»

**Workspace → Models → +**:

- Base model — наш qwen
- System Prompt — правила из `continue-rules.yaml` (список `rules:`, каждое
  правило с новой строки, без дефисов YAML)
- Tools — отметить четыре MCP-сервера
- Capabilities — снять **Builtin Tools**: память, поиск по чатам и заметки
  едят окно модели и не нужны
- Advanced Params — `num_ctx` уже 32768 по умолчанию (`DEFAULT_MODEL_PARAMS`);
  Function Calling — Native

## 7. Проверка

1. Обычный вопрос без поиска — ответ развёрнутый, не «из двух символов»
2. «поищи в базе, как развернуть ...» — вызван `kb_search`, в ответе ссылки
3. «что в dojo по ...» — вызван `dojo_findings`
4. «кто вызывает функцию ...» — вызван инструмент графа
5. Снова `./check-egress.sh --quiet --watch 600` — чисто

## Если не работает

| Симптом | Причина |
|---|---|
| Моделей нет в списке | `docker logs ollama-gate`: если 403 от шлюза — см. строку ниже; если прокси не запущен — нет `OLLAMA_API_KEY`. В Connections адрес должен быть `http://ollama-gate:11435` |
| 401/403 от шлюза | Ключ не в том заголовке: `OLLAMA_AUTH_HEADER=x-api-key`, `OLLAMA_AUTH_PREFIX=` (пустой). После правки `.env` — `docker compose --profile webui up -d ollama-gate` |
| MCP: нет инструментов | Сервис не поднят (`docker compose ps`) или URL с адресом сервера вместо имени контейнера |
| Ответ обрезан, модель «забыла» найденное | Окно: `num_ctx` в параметрах модели |
| Open WebUI что-то качает при старте | Том подменён папкой хоста — вшитые модели закрыты. Только именованный том |

## Что закрыто и почему

- Плагины (Tools/Functions — свой Python на сервере) и `pip install` из них
  выключены (`ENABLE_PLUGINS=false`); MCP от этого не зависят
- Open WebUI в своей сети `webui` (internal): нет маршрута в интернет, нет
  Qdrant; видны только MCP, `ollama-gate`, `webui-tls`
- `ollama-gate` пускает только белый список путей (спрашивать модель),
  GET/POST, без редиректов, без прокси из окружения
- Регистрация закрыта с первого старта, админ — из `.env`

## Чего здесь нет

- Своего RAG Open WebUI (загрузка файлов, Knowledge): поиск — только наши MCP
- Подтверждения вызова инструмента, как «Ask first» в Continue: модель зовёт
  сама. Все инструменты только на чтение, поэтому принимаем
- TLS у остальных портов стека (MCP, Qdrant) — там по-прежнему HTTP
