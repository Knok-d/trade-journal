# Деплой Mini App

Роли разнесены жёстко:

| Где | Что делает | Какие секреты |
|---|---|---|
| Мак | backfill с Bybit, сверка, `export` | ключ биржи (Keychain) |
| Сервер | отдаёт Mini App, хранит журнал | только токен бота |

**Ключи биржи на сервер не попадают ни в каком виде.** Туда едут посчитанные
сделки, а обратно ничего не едет: заметки живут на сервере.

## Предусловия

- A-запись домена указывает на сервер (проверить: `dig +short <домен> A`)
- Открыты порты 80 (проверка владения Let's Encrypt) и 443
- Вход по SSH только по ключу, парольная аутентификация выключена

Сертификат у регистратора покупать не нужно: Caddy получает Let's Encrypt
бесплатно и продлевает сам.

## Установка

```sh
# 1. Пользователь без прав и каталоги (на сервере)
adduser --system --group --home /opt/trade-journal --no-create-home tradejournal
mkdir -p /opt/trade-journal /var/lib/trade-journal /etc/trade-journal
chown tradejournal:tradejournal /opt/trade-journal /var/lib/trade-journal
chmod 750 /var/lib/trade-journal && chmod 700 /etc/trade-journal
```

```sh
# 2. Код — rsync с мака, а не git clone: репозиторий приватный, и деплой-ключ
#    на сервере был бы ещё одним доступом, который переживает смену пароля
rsync -az --delete --exclude '.git' --exclude '__pycache__' \
      --exclude '*.pyc' --exclude '*.db' ./ root@СЕРВЕР:/opt/trade-journal/
ssh root@СЕРВЕР 'chown -R tradejournal:tradejournal /opt/trade-journal'
```

```sh
# 3. Секреты — прямым каналом Keychain → SSH, чтобы значение не попало
#    ни в аргументы команды, ни в историю шелла (запускать на маке)
{
  printf 'TRADE_JOURNAL_TELEGRAM_TOKEN='
  security find-generic-password -s trade-journal -a telegram-token -w
  printf 'TRADE_JOURNAL_TELEGRAM_CHAT_ID=%s\n' \
    "$(security find-generic-password -s trade-journal -a telegram-chat-id -w)"
  printf 'TRADE_JOURNAL_DB=/var/lib/trade-journal/journal.db\n'
} | ssh root@СЕРВЕР 'cat > /etc/trade-journal/env
                     chown root:tradejournal /etc/trade-journal/env
                     chmod 640 /etc/trade-journal/env'
```

```sh
# 4. Сервисы: Mini App и бот (на сервере)
cp /opt/trade-journal/deploy/trade-journal-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trade-journal-miniapp trade-journal-bot
```

> Бот должен работать **ровно в одном месте**: Telegram отдаёт длинный опрос
> одному потребителю, второй получает 409 и они отбирают апдейты друг у друга.
> Раз база живёт на сервере, то и бот там — иначе он читал бы другую копию.

```sh
# 5. HTTPS. В Ubuntu 22.04 Caddy нет в стандартных репозиториях
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy

cp /opt/trade-journal/deploy/Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
```

**Подключать Caddy только когда A-запись уже указывает на сервер.** Проверка
Let's Encrypt по несуществующей записи проваливается и тратит попытку из лимита
(5 отказов на домен в час). Убедиться заранее:
`dig +short ДОМЕН A @8.8.8.8` должен вернуть IP сервера.

База лежит в `/var/lib/trade-journal/journal.db`; путь задаёт `TRADE_JOURNAL_DB`
в юните. Любая команда, запущенная руками, обязана задать ту же переменную —
без неё путь выводится из `HOME`, а он у сервиса и у `sudo` разный.

## Первое заполнение данными

```sh
# на маке: выгрузка вместе с уже накопленными разборами
python3 -m journal.cli export /tmp/transfer.db --with-journal
scp /tmp/transfer.db root@СЕРВЕР:/tmp/

# на сервере
sudo -u tradejournal TRADE_JOURNAL_DB=/var/lib/trade-journal/journal.db \
     python3 -m journal.cli import /tmp/transfer.db
rm /tmp/transfer.db
```

Дальнейшие синхронизации — без `--with-journal`: заметки уже живут на сервере,
и заливка их не трогает.

## Автосинхронизация (на маке)

Сервер не может обновиться сам: ключи биржи есть только на маке. Поэтому
расписание живёт там — `LaunchAgent`, а не `LaunchDaemon`: демон стартует до
входа в систему и до связки ключей не дотянется.

```sh
PLIST=~/Library/LaunchAgents/com.knokd.trade-journal-sync.plist
sed -e "s|REPLACE_WITH_PROJECT|$HOME/dev/trade-journal|g" \
    -e "s|REPLACE_WITH_HOME|$HOME|g" \
    -e "s|REPLACE_WITH_REMOTE|root@СЕРВЕР|g" \
    deploy/com.knokd.trade-journal-sync.plist > "$PLIST"
launchctl load "$PLIST"
launchctl list | grep trade-journal   # второе поле — код выхода, 0 = успех
```

Раз в 30 минут: backfill за 7 дней → пересборка → выгрузка → заливка на сервер.
Перекрытие в 7 дней намеренное — оно закрывает дыру, если мак был выключен.
Лог: `~/Library/Logs/trade-journal-sync.log`.

Нет сети — скрипт молча выходит с кодом 0: мак в дороге это норма, следующий
запуск догонит. А вот отказ сервера принять данные завершается ненулевым кодом
и виден в `launchctl list`.

### Почему в интерфейсе есть отметка свежести

Сломавшийся синк не выглядит поломкой: дневник показывает позавчерашние цифры
так же уверенно, как сегодняшние. Поэтому момент последней выгрузки едет вместе
с данными, и все три интерфейса (дашборд, Mini App, бот) показывают
предупреждение, если данным больше `stats.STALE_AFTER_HOURS` часов.

## Обновление кода на сервере

`sync.sh` переносит **данные, а не код**. После изменений в репозитории:

```sh
rsync -az --delete --exclude '.git' --exclude '__pycache__' \
      --exclude '*.pyc' --exclude '*.db' ./ root@СЕРВЕР:/opt/trade-journal/
ssh root@СЕРВЕР 'chown -R tradejournal:tradejournal /opt/trade-journal
                 cd /opt/trade-journal && python3 -m unittest discover -s tests -t .
                 systemctl restart trade-journal-miniapp trade-journal-bot'
```

Тесты на сервере — не формальность: там Python 3.10 против свежего на маке,
и синтаксис новее 3.10 туда не доедет.

## Кнопка в боте

Ставится через API, без похода в BotFather (на маке):

```sh
python3 - <<'PY'
import json, urllib.request
from journal import keychain
token = keychain.get("telegram-token")
payload = json.dumps({"menu_button": {
    "type": "web_app", "text": "Дневник",
    "web_app": {"url": "https://ДОМЕН/"}}}).encode()
req = urllib.request.Request(f"https://api.telegram.org/bot{token}/setChatMenuButton",
                             data=payload, headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req)))
PY
```

## Проверка после установки

```sh
curl -s -o /dev/null -w "%{http_code}\n" https://<домен>/api/summary   # ждём 401
curl -s -o /dev/null -w "%{http_code}\n" https://<домен>/               # ждём 200
```

**401 на API без подписи — это и есть главная проверка.** Если там 200,
историю торговли отдают любому, кто узнал адрес: немедленно остановить сервис.

## Откат

```sh
systemctl stop trade-journal-miniapp
systemctl disable trade-journal-miniapp
```

База остаётся в `/var/lib/trade-journal/` — при снятии сервиса её надо либо
забрать (`export` + `scp` на мак), либо удалить осознанно.
