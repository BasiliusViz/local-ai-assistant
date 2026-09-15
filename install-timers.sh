#!/bin/sh
# Поставить расписание обновления Confluence и Jira через таймеры systemd.
#
#   ./install-timers.sh --dry-run        показать юниты, ничего не меняя
#   sudo ./install-timers.sh             поставить
#   sudo ./install-timers.sh --remove    убрать расписание
#
# Замена install-cron.sh для серверов, где нет cron: на минимальных установках
# его часто не ставят, а systemd есть почти всегда. Ставить ничего не нужно.
#
# По сравнению с cron у таймеров два преимущества именно здесь:
#   - Persistent=true: если сервер был выключен в момент запуска, пропущенное
#     обновление выполнится при включении. Cron его просто теряет
#   - логи идут в системный журнал (journalctl -u local-ai-jira), не нужно
#     думать, куда перенаправлять вывод и есть ли права писать в /var/log
#
# Сами обновления — те же confluence/cron.sh и jira/cron.sh, меняется только
# то, что их запускает. Расписание то же: Confluence в начале чётного часа,
# Jira в половине, сверка Jira ночью в 3:15.
#
# Юниты системные и выполняются от root. У root гарантированно есть доступ к
# docker, а пользовательские таймеры без отдельной настройки перестают
# срабатывать, как только пользователь выходит из системы.

set -eu

# Подменяются при проверке скрипта; в работе всегда значения по умолчанию
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SKIP_ROOT_CHECK="${SKIP_ROOT_CHECK:-0}"

ROOT="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-install}"

# имя юнита | расписание | скрипт | аргументы | описание
JOBS="local-ai-confluence|*-*-* 00/2:00:00|$ROOT/confluence/cron.sh||Обновление Confluence
local-ai-jira|*-*-* 00/2:30:00|$ROOT/jira/cron.sh||Обновление Jira
local-ai-jira-prune|*-*-* 03:15:00|$ROOT/jira/cron.sh|--prune|Сверка Jira: убрать задачи вне охвата"

service_unit() {
    # $1 скрипт, $2 аргументы, $3 описание
    cat <<EOF
[Unit]
Description=$3 (local-ai)
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
ExecStart="$1" $2
# У oneshot-сервиса по умолчанию таймаута нет вовсе: зависший прогон (модель
# или Jira перестали отвечать) висел бы вечно, а таймер не запускает новый,
# пока старый не закончился. Три часа — с запасом даже на первую индексацию
TimeoutStartSec=3h
EOF
}

timer_unit() {
    # $1 расписание, $2 описание
    cat <<EOF
[Unit]
Description=$2 по расписанию (local-ai)

[Timer]
OnCalendar=$1
# Пропущенный из-за выключенного сервера запуск выполнится при включении
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
}

require_root() {
    if [ "$SKIP_ROOT_CHECK" != "1" ] && [ "$(id -u)" != "0" ]; then
        echo "Нужны права администратора: юниты systemd пишутся в $UNIT_DIR."
        echo "Запустите так:  sudo $0 ${1:-}"
        exit 1
    fi
}

if [ "$MODE" = "--dry-run" ]; then
    echo "Каталог проекта: $ROOT"
    echo "Юниты лягут в:   $UNIT_DIR (ничего не записано)"
    printf '%s\n' "$JOBS" | while IFS='|' read -r name when script args desc; do
        echo
        echo "===== $name.service ====="
        service_unit "$script" "$args" "$desc"
        echo
        echo "===== $name.timer ====="
        timer_unit "$when" "$desc"
    done
    exit 0
fi

require_root

if [ "$SKIP_ROOT_CHECK" != "1" ]; then
    if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
        echo "На этом сервере нет systemd — таймеры поставить нельзя."
        echo "Остаётся cron: поставьте пакет (cron или cronie) и запустите ./install-cron.sh"
        exit 1
    fi
fi

if [ "$MODE" = "--remove" ]; then
    printf '%s\n' "$JOBS" | while IFS='|' read -r name when script args desc; do
        $SYSTEMCTL disable --now "$name.timer" >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/$name.service" "$UNIT_DIR/$name.timer"
        echo "убрано: $name"
    done
    $SYSTEMCTL daemon-reload
    echo "Расписание снято. Сами скрипты обновления не тронуты, их можно запускать руками."
    exit 0
fi

if [ "$MODE" != "install" ]; then
    echo "Неизвестный режим: $MODE. Бывают: --dry-run, --remove или без аргументов."
    exit 2
fi

# Проверить расписания до записи: опечатка в OnCalendar не роняет установку,
# а тихо даёт таймер, который никогда не сработает
if command -v systemd-analyze >/dev/null 2>&1; then
    printf '%s\n' "$JOBS" | while IFS='|' read -r name when script args desc; do
        if ! systemd-analyze calendar "$when" >/dev/null 2>&1; then
            echo "Неверное расписание у $name: $when"
            exit 1
        fi
    done
fi

chmod +x "$ROOT/confluence/cron.sh" "$ROOT/jira/cron.sh"

printf '%s\n' "$JOBS" | while IFS='|' read -r name when script args desc; do
    service_unit "$script" "$args" "$desc" > "$UNIT_DIR/$name.service"
    timer_unit "$when" "$desc" > "$UNIT_DIR/$name.timer"
    echo "записано: $name.service, $name.timer"
done

$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now local-ai-confluence.timer local-ai-jira.timer local-ai-jira-prune.timer

echo
echo "Расписание установлено. Ближайшие запуски:"
$SYSTEMCTL list-timers 'local-ai-*' --all || true
echo
echo "Логи прогонов:     journalctl -u local-ai-jira -n 50"
echo "Запустить сейчас:  sudo systemctl start local-ai-jira.service"
echo "Убрать расписание: sudo $0 --remove"
