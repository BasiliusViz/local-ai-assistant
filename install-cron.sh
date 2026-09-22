#!/bin/sh
# Поставить расписание обновления Confluence, DefectDojo, Jira и кода в cron.
#
#   ./install-cron.sh             поставить
#   ./install-cron.sh --dry-run   показать, что получится, ничего не меняя
#
# Чужие задачи в crontab не трогает: убирает только свои старые строки (те, где
# есть confluence/cron.sh, dojo/cron.sh, jira/cron.sh или repos/cron.sh) и дописывает
# актуальные. Пометку MARK не переименовывать: по ней находится и убирается
# строка, поставленная прошлой версией скрипта. Поэтому
# запускать повторно безопасно — например, когда в репозитории поменялось
# расписание, — дублей не будет.
#
# Ставить от того пользователя, у которого есть доступ к docker. Если docker
# вы запускаете через sudo — запускайте и этот скрипт через sudo, иначе cron
# будет запускать обновление от пользователя без прав, и оно молча не сработает.
# Но код (repos/cron.sh) клонирует от того же пользователя: поставите от root —
# новые клоны в CODE_DIR будут root-овыми, и без sudo их не удалить.
#
# Куда пишется лог. Обычному пользователю писать в /var/log нельзя, и cron
# ничего не сообщит об ошибке — просто не будет записей. Поэтому от root лог
# идёт в /var/log, от остальных — в ~/logs.

set -eu

# Подменяется заглушкой при проверке скрипта, в работе всегда crontab
CRONTAB="${CRONTAB:-crontab}"

# На минимальных серверных установках cron нет вовсе. Сказать об этом сразу и
# показать замену — а не упасть посреди записи с невнятным «not found»
if ! command -v "${CRONTAB%% *}" >/dev/null 2>&1; then
    echo "На этом сервере нет cron: команда crontab не найдена."
    echo "Ставить ничего не нужно — то же расписание делается таймерами systemd:"
    echo "    sudo ./install-timers.sh"
    exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
MARK="# local-ai: обновление Confluence и Jira (install-cron.sh)"

if [ "$(id -u)" = "0" ]; then
    LOG_DIR=/var/log
else
    LOG_DIR="$HOME/logs"
fi

our_lines() {
    echo "$MARK"
    echo "0 */2 * * * $ROOT/confluence/cron.sh >> $LOG_DIR/local-ai-confluence.log 2>&1"
    echo "15 */2 * * * $ROOT/dojo/cron.sh >> $LOG_DIR/local-ai-dojo.log 2>&1"
    echo "30 */2 * * * $ROOT/jira/cron.sh >> $LOG_DIR/local-ai-jira.log 2>&1"
    echo "15 3 * * * $ROOT/jira/cron.sh --prune >> $LOG_DIR/local-ai-jira.log 2>&1"
    # Код — раз в сутки: его индексация пока пересчитывает всё целиком
    echo "0 2 * * * $ROOT/repos/cron.sh >> $LOG_DIR/local-ai-code.log 2>&1"
}

# Текущее расписание. Если его ещё нет вовсе, crontab -l завершается ошибкой —
# это не повод падать, просто начинаем с пустого
current="$($CRONTAB -l 2>/dev/null || true)"

# Всё чужое остаётся как было; наши прошлые строки и пометка — убираются
kept="$(printf '%s\n' "$current" | grep -v -e '/confluence/cron.sh' -e '/jira/cron.sh' -e '/dojo/cron.sh' -e '/repos/cron.sh' -e "^$MARK\$" || true)"

result="$(
    if [ -n "$kept" ]; then
        printf '%s\n' "$kept"
    fi
    our_lines
)"

echo "Каталог проекта: $ROOT"
echo "Логи:            $LOG_DIR"
echo

if [ "${1:-}" = "--dry-run" ]; then
    echo "Так будет выглядеть расписание (ничего не записано):"
    echo "----------------------------------------------------"
    printf '%s\n' "$result"
    exit 0
fi

# Без доступа к docker расписание встанет, но работать не будет — лучше
# сказать сейчас, чем искать потом, почему лог пустой
if ! docker compose version >/dev/null 2>&1; then
    echo "[!] У пользователя $(id -un) нет доступа к docker compose."
    echo "    Расписание ставлю, но запускаться оно не сможет. Поставьте от"
    echo "    пользователя с доступом к docker или через sudo."
    echo
fi

if [ "$(id -u)" = "0" ]; then
    echo "[!] Ставлю от root: обновление кода (repos/cron.sh) тоже пойдёт от root,"
    echo "    и новые клоны в CODE_DIR будут принадлежать root. Если у вашего"
    echo "    пользователя есть доступ к docker — лучше поставить от него."
    echo
fi

mkdir -p "$LOG_DIR"
chmod +x "$ROOT/confluence/cron.sh" "$ROOT/jira/cron.sh" "$ROOT/dojo/cron.sh" "$ROOT/repos/cron.sh"

printf '%s\n' "$result" | $CRONTAB -

echo "Расписание установлено:"
echo "-----------------------"
$CRONTAB -l
echo
echo "Первые записи в логах появятся в ближайший чётный час (Confluence) и в"
echo "половине часа (Jira), код — в 2:00. Смотреть: tail -f $LOG_DIR/local-ai-jira.log"
