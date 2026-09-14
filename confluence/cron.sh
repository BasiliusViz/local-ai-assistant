#!/bin/sh
# Обновление Confluence по расписанию: выгрузка изменившихся страниц и
# индексация. Вешается на cron, например раз в час:
#
#   0 * * * * /srv/local-ai/confluence/cron.sh >> /var/log/local-ai-confluence.log 2>&1
#
# Обе стадии инкрементальные: выгрузка берёт только страницы с новой версией,
# индексатор пересчитывает только файлы с изменившимся содержимым. Поэтому
# прогон, в котором ничего не поменялось, занимает секунды.
#
# Защита от наложения: если прошлый прогон ещё идёт (скажем, после большой
# правки пространства индексация затянулась дольше часа), новый не стартует.
# Иначе два индексатора пишут в одни и те же точки одновременно, а эмбеддер
# получает двойную нагрузку.

set -eu

cd "$(dirname "$0")/.."

LOCK=/tmp/local-ai-confluence.lock
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') прошлый прогон ещё идёт, пропускаю"
    exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') выгрузка"
docker compose exec -T kb python confluence/sync.py

echo "$(date '+%Y-%m-%d %H:%M:%S') индексация"
docker compose exec -T kb python -m kb.doc_index /docs/confluence --source confluence

echo "$(date '+%Y-%m-%d %H:%M:%S') готово"
