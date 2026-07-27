#!/bin/bash
# Синхронизация мак -> сервер. Запускается по расписанию из launchd.
#
# Ключи биржи есть только на маке, поэтому к Bybit ходит он, а сервер получает
# уже посчитанные сделки. Разборы едут в обратную сторону? Нет: они живут на
# сервере и заливкой не затрагиваются (см. journal/sync.py).
#
# Скрипт обязан быть безопасным при любом обрыве: неудача на любом шаге
# оставляет обе стороны в прежнем состоянии, а не в половинчатом.

set -euo pipefail

# Адрес сервера задаётся снаружи и намеренно не имеет умолчания: репозиторий
# публичный, и своя инфраструктура в нём светиться не должна.
REMOTE="${TRADE_JOURNAL_REMOTE:?задай TRADE_JOURNAL_REMOTE, например root@example.com}"
REMOTE_DB="${TRADE_JOURNAL_REMOTE_DB:-/var/lib/trade-journal/journal.db}"
REMOTE_PROJECT="${TRADE_JOURNAL_REMOTE_PROJECT:-/opt/trade-journal}"
PROJECT="${TRADE_JOURNAL_PROJECT:-$HOME/dev/trade-journal}"
DAYS="${TRADE_JOURNAL_SYNC_DAYS:-7}"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

cd "$PROJECT"

# Файл переноса — во временном каталоге, с гарантированной уборкой:
# он содержит всю историю сделок и не должен валяться после падения.
TRANSFER="$(mktemp -t trade-journal-sync)"
cleanup() { rm -f "$TRANSFER"; }
trap cleanup EXIT

# Сеть может отсутствовать (мак в дороге) — это не ошибка, а обычное дело.
# Тихо выходим, следующий запуск догонит.
if ! ping -c1 -W2000 api.bybit.com >/dev/null 2>&1; then
  log "сети нет, пропускаю"
  exit 0
fi

log "бэкфилл за $DAYS дн."
python3 -m journal.cli backfill --days "$DAYS" >/dev/null

log "пересборка"
python3 -m journal.cli rebuild >/dev/null

log "выгрузка"
python3 -m journal.cli export "$TRANSFER" >/dev/null

# BatchMode: без пароля и без интерактива — иначе задание из launchd
# зависнет навсегда, ожидая ввода, которого никто не увидит.
log "передача на $REMOTE"
scp -q -o BatchMode=yes -o ConnectTimeout=15 "$TRANSFER" "$REMOTE:/tmp/tj-sync.db"

log "слияние на сервере"
# Только && между шагами: через `;` код возврата брался бы от rm, и провал
# импорта выглядел бы успехом. Уборка временного файла — в отдельном вызове,
# чтобы она случилась при любом исходе, но не подменяла собой результат.
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
     "cd $REMOTE_PROJECT && sudo -u tradejournal TRADE_JOURNAL_DB=$REMOTE_DB \
        python3 -m journal.cli import /tmp/tj-sync.db"; then
  ssh -o BatchMode=yes "$REMOTE" 'rm -f /tmp/tj-sync.db' || true
  log "готово"
else
  status=$?
  ssh -o BatchMode=yes "$REMOTE" 'rm -f /tmp/tj-sync.db' || true
  log "ПРОВАЛ: сервер не принял данные (код $status)"
  exit "$status"
fi
