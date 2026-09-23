#!/usr/bin/env bash
# Настройка Continue в VS Code для локального ассистента (Linux, macOS).
#
# Что делает:
#   1. ставит расширение Continue (из Marketplace или из .vsix);
#   2. отключает телеметрию Continue в настройках VS Code;
#   3. собирает ~/.continue/config.yaml из continue-config.example.yaml,
#      подставляя адреса, токен и модель (старый конфиг сохраняется рядом);
#   4. проверяет связь с моделью и с MCP-серверами.
#
# Чего НЕ делает (вручную, раздел 3 в guides/USER-GUIDE.md): перезапуск VS Code,
# режим Agent и политика инструментов Automatic — Continue хранит её у себя
# внутри, а не в файле.
#
# Пример:
#   ./setup-continue.sh --ollama http://gpu-01.corp:11434 --server http://ai-kb.corp --vsix ~/soft/continue.vsix
#
# Токен не передавать в командной строке — останется в истории. Скрипт
# спросит его сам (ввод скрыт), или --vault-path при наличии vault CLI.
#
# Нужны: bash, curl, python3.

set -u

OLLAMA="" SERVER="" TOKEN="" VAULT_PATH="" VAULT_FIELD="token" NO_TOKEN=0
VSIX="" MODEL="" CONTEXT="" NO_DOJO=0 SKIP_EXT=0
TEMPLATE="$(cd "$(dirname "$0")" && pwd)/continue-config.example.yaml"

usage() {
  cat <<'EOF'
Использование: ./setup-continue.sh --ollama URL --server URL [параметры]

  --ollama URL          сервер с моделью (порт по умолчанию 11434)
  --server URL          сервер поиска, без порта
  --vault-path PATH     взять токен из Vault (нужен vault CLI и vault login)
  --vault-field NAME    поле с токеном в секрете (по умолчанию token)
  --no-token            модель без шлюза, заголовок x-api-key не писать
  --vsix FILE           поставить Continue из файла (Marketplace закрыт)
  --model NAME          модель чата (по умолчанию — как в образце)
  --context N           contextLength (по умолчанию — как в образце)
  --no-dojo             без сервера DefectDojo (порт 8012)
  --skip-extension      не ставить расширение
  --template FILE       образец конфига (по умолчанию рядом со скриптом)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ollama) OLLAMA="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    --vault-path) VAULT_PATH="$2"; shift 2 ;;
    --vault-field) VAULT_FIELD="$2"; shift 2 ;;
    --no-token) NO_TOKEN=1; shift ;;
    --vsix) VSIX="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --context) CONTEXT="$2"; shift 2 ;;
    --no-dojo) NO_DOJO=1; shift ;;
    --skip-extension) SKIP_EXT=1; shift ;;
    --template) TEMPLATE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $1"; usage; exit 1 ;;
  esac
done

[ -n "$OLLAMA" ] && [ -n "$SERVER" ] || { usage; exit 1; }

FAILS=0
if [ -t 1 ]; then G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' C=$'\e[36m' N=$'\e[0m'; else G= Y= R= C= N=; fi
ok()   { echo "  ${G}[OK]${N}   $*"; }
warn() { echo "  ${Y}[!]${N}    $*"; }
bad()  { echo "  ${R}[FAIL]${N} $*"; FAILS=$((FAILS + 1)); }
step() { echo; echo "${C}== $*${N}"; }

command -v python3 >/dev/null || { echo "Нужен python3"; exit 1; }
command -v curl >/dev/null || { echo "Нужен curl"; exit 1; }

# Адреса: схема по умолчанию http, у модели порт 11434, у поиска порт отрезаем
norm() { local u="${1%/}"; case "$u" in *://*) ;; *) u="http://$u" ;; esac; echo "$u"; }
OLLAMA="$(norm "$OLLAMA")"
echo "$OLLAMA" | grep -Eq ':[0-9]+$' || OLLAMA="$OLLAMA:11434"
SERVER="$(norm "$SERVER")"
SERVER="$(echo "$SERVER" | sed -E 's/:[0-9]+$//')"

# ---------------------------------------------------------------- token
step "Токен"
if [ "$NO_TOKEN" = 1 ]; then
  warn "режим без токена: заголовок x-api-key в конфиг не пишется"
else
  if [ -z "$TOKEN" ] && [ -n "$VAULT_PATH" ]; then
    if command -v vault >/dev/null; then
      if TOKEN="$(vault kv get -field="$VAULT_FIELD" "$VAULT_PATH" 2>/dev/null)" && [ -n "$TOKEN" ]; then
        ok "взят из Vault: $VAULT_PATH"
      else
        warn "из Vault прочитать не удалось (vault login выполнен? VAULT_ADDR задан?)"; TOKEN=""
      fi
    else
      warn "vault CLI не найден — введите токен вручную"
    fi
  fi
  if [ -z "$TOKEN" ]; then
    read -rsp "Токен из Vault (ввод скрыт): " TOKEN; echo
  fi
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '\r\n' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
  [ -n "$TOKEN" ] || { bad "токен пустой"; exit 1; }
  # Кириллица или пробел в заголовке ломают запрос в глубине клиента
  if printf '%s' "$TOKEN" | LC_ALL=C grep -q '[^!-~]'; then
    bad "в токене пробелы или не-латиница — скопирован не целиком или не тот"; exit 1
  fi
  ok "токен принят (${#TOKEN} символов)"
fi

# ---------------------------------------------------------------- extension
step "Расширение Continue"
has_continue() { code --list-extensions 2>/dev/null | grep -qix 'continue.continue'; }
if [ "$SKIP_EXT" = 1 ]; then
  warn "пропущено (--skip-extension)"
elif ! command -v code >/dev/null; then
  bad "VS Code не найден (нет команды code). Установите VS Code или добавьте code в PATH"
elif has_continue && [ -z "$VSIX" ]; then
  ok "уже установлено"
else
  if [ -n "$VSIX" ]; then
    if [ -f "$VSIX" ]; then code --install-extension "$VSIX" --force >/dev/null 2>&1
    else bad "файл не найден: $VSIX"; fi
  else
    code --install-extension Continue.continue >/dev/null 2>&1
  fi
  if has_continue; then ok "установлено"
  elif [ -z "$VSIX" ]; then bad "не установилось. Marketplace закрыт? Укажите файл: --vsix путь/continue.vsix"
  else bad "не установилось из $VSIX"; fi
fi

# ---------------------------------------------------------------- telemetry
step "Телеметрия Continue"
if [ "$(uname)" = "Darwin" ]; then
  SETTINGS="$HOME/Library/Application Support/Code/User/settings.json"
else
  SETTINGS="${XDG_CONFIG_HOME:-$HOME/.config}/Code/User/settings.json"
fi
# settings.json — JSON с комментариями: вставляем текст, а не пересобираем
SETTINGS="$SETTINGS" python3 - >/dev/null <<'PY'
import os, re, shutil, sys
p = os.environ["SETTINGS"]
line = '"continue.telemetryEnabled": false'
if not os.path.exists(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write("{\n  %s\n}\n" % line)
    print("created"); sys.exit(0)
t = open(p, encoding="utf-8-sig").read()
if re.search(r'"continue\.telemetryEnabled"\s*:\s*false', t):
    print("already"); sys.exit(0)
if re.search(r'"continue\.telemetryEnabled"\s*:\s*true', t):
    new = re.sub(r'("continue\.telemetryEnabled"\s*:\s*)true', r'\1false', t)
else:
    i = t.find("{")
    if i < 0:
        print("unparsed"); sys.exit(0)
    rest = t[i + 1:]
    sep = "" if rest.strip().startswith("}") else ","
    new = t[:i + 1] + "\n  " + line + sep + rest
shutil.copyfile(p, p + ".bak")
open(p, "w", encoding="utf-8").write(new)
print("changed")
PY
case "$?" in 0) ;; *) bad "не удалось изменить $SETTINGS";; esac
if grep -q '"continue.telemetryEnabled": *false' "$SETTINGS" 2>/dev/null; then
  ok "выключена ($SETTINGS)"
else
  bad "не разобрал $SETTINGS — выключите вручную: Ctrl+, -> continue telemetry"
fi

# ---------------------------------------------------------------- config
step "Конфиг Continue"
[ -f "$TEMPLATE" ] || { bad "нет образца $TEMPLATE (положите continue-config.example.yaml рядом со скриптом или укажите --template)"; exit 1; }
CFG_DIR="$HOME/.continue"
CFG="$CFG_DIR/config.yaml"
mkdir -p "$CFG_DIR"
if [ -f "$CFG" ]; then
  BAK="$CFG.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$CFG" "$BAK" && ok "прежний конфиг сохранён: $BAK"
fi

# Значения идут через окружение, а не подстановкой в текст скрипта:
# спецсимволы токена не ломают ни sed, ни python
TEMPLATE="$TEMPLATE" OUT="$CFG" OLLAMA="$OLLAMA" SERVER="$SERVER" TOKEN="$TOKEN" \
NO_TOKEN="$NO_TOKEN" MODEL="$MODEL" CONTEXT="$CONTEXT" NO_DOJO="$NO_DOJO" python3 - <<'PY'
import os, re, sys
e = os.environ
lines = [l for l in open(e["TEMPLATE"], encoding="utf-8-sig").read().splitlines()
         if not l.lstrip().startswith("#")]
cfg = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
cfg = cfg.replace("http://АДРЕС-OLLAMA:11434", e["OLLAMA"])
cfg = cfg.replace("http://АДРЕС-СЕРВЕРА", e["SERVER"])
if e["NO_TOKEN"] == "1":
    cfg = re.sub(r"(?m)^\s*(requestOptions|headers):\s*\n", "", cfg)
    cfg = re.sub(r"(?m)^\s*x-api-key: ТОКЕН\s*\n", "", cfg)
else:
    # YAML в одинарных кавычках: экранируется только сама кавычка
    cfg = cfg.replace("x-api-key: ТОКЕН", "x-api-key: '" + e["TOKEN"].replace("'", "''") + "'")
if e["MODEL"]:
    # первая модель в образце — модель чата
    cfg = re.sub(r"(?m)^(\s+- name:\s*).+$", lambda m: m.group(1) + e["MODEL"], cfg, count=1)
    cfg = re.sub(r"(?m)^(\s+model:\s*)\S+", lambda m: m.group(1) + e["MODEL"], cfg, count=1)
if e["CONTEXT"]:
    cfg = re.sub(r"(?m)^(\s+contextLength:\s*)\d+", lambda m: m.group(1) + e["CONTEXT"], cfg)
if e["NO_DOJO"] == "1":
    cfg = re.sub(r"(?m)^  - name: defectdojo\n(    .*\n)+\n?", "", cfg)
if "АДРЕС-" in cfg or "ТОКЕН" in cfg:
    print("в конфиге остались незаполненные места (АДРЕС-/ТОКЕН) — образец изменился")
    sys.exit(1)
old = os.umask(0o077)  # внутри токен: файл только для владельца
try:
    with open(e["OUT"], "w", encoding="utf-8") as f:
        f.write(cfg)
finally:
    os.umask(old)
os.chmod(e["OUT"], 0o600)
PY
if [ $? -ne 0 ]; then bad "конфиг не собран"; exit 1; fi
ok "записан: $CFG"
CHAT_MODEL="$(grep -m1 -E '^\s+model:' "$CFG" | awk '{print $2}')"
ok "модель $CHAT_MODEL на $OLLAMA, поиск на $SERVER"

# ---------------------------------------------------------------- checks
step "Связь с моделью"
HDR=()
[ "$NO_TOKEN" = 1 ] || HDR=(-H "x-api-key: $TOKEN")
# ${HDR[@]+...}: пустой массив под set -u в bash до 4.4 считается неопределённым
RESP="$(curl -s -m 20 -w '\n%{http_code}' ${HDR[@]+"${HDR[@]}"} "$OLLAMA/api/tags")"
CODE="$(echo "$RESP" | tail -n1)"
BODY="$(echo "$RESP" | sed '$d')"
case "$CODE" in
  200)
    NAMES="$(echo "$BODY" | python3 -c 'import sys,json; print(" ".join(m["name"] for m in json.load(sys.stdin).get("models", [])))' 2>/dev/null)"
    ok "сервер отвечает, моделей: $(echo "$NAMES" | wc -w)"
    case " $NAMES " in
      *" $CHAT_MODEL "*|*" $CHAT_MODEL:latest "*) ;;
      *) warn "модели $CHAT_MODEL нет в списке сервера: $NAMES" ;;
    esac ;;
  401|403) bad "доступ запрещён ($CODE) — неверный токен" ;;
  404) warn "шлюз не пропускает /api/tags (404) — не страшно, проверьте вопросом в чате" ;;
  000) bad "нет связи с $OLLAMA" ;;
  *) bad "сервер модели ответил $CODE" ;;
esac

step "Связь с MCP-серверами"
check_mcp() {
  local port="$1" want="$2" out tools
  out="$(curl -s -m 20 -X POST "$SERVER:$port/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
  tools="$(echo "$out" | python3 -c 'import sys,json; print(", ".join(t["name"] for t in json.load(sys.stdin)["result"]["tools"]))' 2>/dev/null)"
  if [ -z "$tools" ]; then
    if [ "$port" = 8012 ]; then
      warn "$port: не отвечает (доступ к DefectDojo открыт не всем; не нужен — запустите с --no-dojo)"
    else
      bad "$port: не отвечает"
    fi
  elif echo ", $tools," | grep -q ", $want,"; then
    ok "$port: $tools"
  else
    bad "$port: отвечает, но нет $want — $tools"
  fi
}
check_mcp 8010 kb_search
check_mcp 8011 get_neighbors
[ "$NO_DOJO" = 1 ] || check_mcp 8012 dojo_findings

# ---------------------------------------------------------------- summary
echo
if [ "$FAILS" = 0 ]; then
  echo "${G}Готово. Осталось вручную (guides/USER-GUIDE.md, раздел 3):${N}"
else
  echo "${R}Ошибок: $FAILS. Исправьте и запустите скрипт ещё раз. Дальше вручную:${N}"
fi
echo "  1. Закрыть VS Code полностью и открыть снова"
echo "  2. Панель Continue -> ассистент Local Assistant -> режим Agent"
echo "  3. Значок инструментов в строке ввода -> knowledge-base, code-graph, defectdojo -> Automatic"
echo "  4. Проверочные вопросы из раздела 4"
exit "$FAILS"
