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
PULLBACK="$(mktemp -t trade-journal-pull)"
cleanup() { rm -f "$TRANSFER" "$PULLBACK"; }
trap cleanup EXIT

# Сеть может отсутствовать (мак в дороге) — это не ошибка, а обычное дело.
# Тихо выходим, следующий запуск догонит.
#
# Код 75 (EX_TEMPFAIL), а не 0: пропущенный круг НЕ синхронизация. С нулём
# приложение засчитывало его успехом и рисовало «Данные свежие» поверх цифр
# любой давности — captive-портал в кафе или сеть без ICMP давали бы дневник,
# который бодро врёт. Ровно тот отказ, который README называет худшим.
if ! ping -c1 -W2000 api.bybit.com >/dev/null 2>&1; then
  log "сети нет, пропускаю"
  exit 75
fi

# Инкрементально: круг идёт раз в минуту, и тянуть неделю истории каждый раз
# незачем. `--days` остаётся верхней границей на случай, если край выкачки
# потерялся или мак не включали дольше недели.
log "бэкфилл"
python3 -m journal.cli backfill --days "$DAYS" --since-last >/dev/null

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
else
  status=$?
  ssh -o BatchMode=yes "$REMOTE" 'rm -f /tmp/tj-sync.db' || true
  log "ПРОВАЛ: сервер не принял данные (код $status)"
  exit "$status"
fi

# Обратный рейс. Разбор пишется и с телефона, поэтому журнал едет назад:
# при слиянии побеждает более свежая правка, а не сторона.
#
# Отдельный файл и --journal-only: тащить обратно семь мегабайт сырых fills
# ради пары заметок незачем, а отметка свежести с сервера затёрла бы маковскую
# (на биржу ходит мак, и знает об отставании только он).
log "журнал с сервера"
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" \
     "cd $REMOTE_PROJECT && sudo -u tradejournal TRADE_JOURNAL_DB=$REMOTE_DB \
        python3 -m journal.cli export /tmp/tj-journal.db --journal-only" >/dev/null; then
  scp -q -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE:/tmp/tj-journal.db" "$PULLBACK"
  ssh -o BatchMode=yes "$REMOTE" 'rm -f /tmp/tj-journal.db' || true
  python3 -m journal.cli import "$PULLBACK"
  log "готово"
else
  status=$?
  ssh -o BatchMode=yes "$REMOTE" 'rm -f /tmp/tj-journal.db' || true
  log "ПРОВАЛ: журнал с сервера не забрался (код $status)"
  exit "$status"
fi
