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
# на маке: сделки и журнал одной выгрузкой
python3 -m journal.cli export /tmp/transfer.db
scp /tmp/transfer.db root@СЕРВЕР:/tmp/

# на сервере
sudo -u tradejournal TRADE_JOURNAL_DB=/var/lib/trade-journal/journal.db \
     python3 -m journal.cli import /tmp/transfer.db
rm /tmp/transfer.db
```

Дальше то же самое делает `sync.sh` раз в минуту, плюс обратный рейс
(`export --journal-only` на сервере). Отдельного флага «взять ещё и журнал»
нет намеренно: он был, и ровно там его забывали — заметка с мака тихо не
доезжала до телефона, и заметить это можно было только случайно.

## Приложение на маке (оно же автосинхронизация)

Сервер не может обновиться сам: ключи биржи есть только на маке. Поэтому и
дневник, и расписание живут там — одним процессом `journal.cli app`. Это
`LaunchAgent`, а не `LaunchDaemon`: демон стартует до входа в систему и до
связки ключей не дотянется.

```sh
PLIST=~/Library/LaunchAgents/com.knokd.trade-journal-app.plist
sed -e "s|/Users/dmitry|$HOME|g" \
    -e "s|root@example.com|root@СЕРВЕР|g" \
    deploy/com.knokd.trade-journal-app.plist > "$PLIST"
launchctl load "$PLIST"
launchctl list | grep trade-journal   # второе поле — код выхода, 0 = успех
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8321/   # ждём 200
```

Прошлый агент (`com.knokd.trade-journal-sync`) заменяется этим и должен быть
снят, иначе синка будет два:

```sh
launchctl unload ~/Library/LaunchAgents/com.knokd.trade-journal-sync.plist
rm ~/Library/LaunchAgents/com.knokd.trade-journal-sync.plist
```

Раз в минуту: инкрементальный backfill → пересборка → заливка на сервер →
журнал с сервера обратно. Тянется только новое, от края прошлой выкачки
(`sync_state`) с перекрытием в 15 минут: опоздавший fill не должен провалиться
в дыру между окнами. `--days 7` остаётся верхней границей на случай, если край
потерялся или мак не включали неделю. Лог: `~/Library/Logs/trade-journal-sync.log`.

Нет сети — скрипт молча выходит с кодом 0: мак в дороге это норма, следующий
круг догонит. Отказ сервера завершается ненулевым кодом, и причина видна в
интерфейсе строкой состояния.

> **Почему `KeepAlive`, а не `StartInterval`.** Прошлый агент просыпался раз в
> 30 минут и имел `ProcessType = Background`. Такие задачи система откладывает,
> а пропущенный за сон мака запуск не догоняется сразу: 28.07 данные отстали на
> 3 ч 45 мин при формально исправном агенте (`last exit code = 0`). Процесс,
> который просто живёт, этой ямы не имеет — заснул мак, поток проснулся вместе
> с ним.

### Приложение в /Applications

```sh
mac/build.sh --install
```

Собирает `Trade Journal.app` — окно с `WKWebView` на `127.0.0.1:8321`, своё
меню, своя иконка. Xcode не нужен: `swiftc`, `iconutil` и `codesign` входят в
Command Line Tools, так что внешних зависимостей проект по-прежнему не имеет.
Иконка не лежит картинкой, а рисуется кодом (`mac/make-icon.swift`) — бинарных
файлов в репозитории нет ни одного, и её видно в диффе.

Оболочка сервер не запускает: это дело агента, а две сущности, управляющие
одним процессом, рано или поздно разойдутся. Пока сервера нет, окно пишет, что
ждёт его, и стучится раз в две секунды — после логина агент стартует не мгновенно.

> **После правок в коде агент надо перезапустить.** Приложение держит
> импортированные модули в памяти, поэтому правки в `journal/*.py` подхватятся
> только новым процессом (файлы в `journal/web/` отдаются с диска и обновляются
> перезагрузкой страницы):
>
> ```sh
> launchctl kickstart -k gui/$(id -u)/com.knokd.trade-journal-app
> ```

Раньше здесь было веб-приложение Safari («Добавить в Dock»). Оно работало, но
оставалось контейнером Safari: `CFBundleIdentifier` вида
`com.apple.Safari.WebApp.<UUID>`, имя из `<title>` и вместо иконки монограмма,
потому что у страницы её нет. Старое приложение, если оно осталось, лежит в
`~/Applications` и удаляется как обычное.

### Почему в интерфейсе есть отметка свежести

Сломавшийся синк не выглядит поломкой: дневник показывает позавчерашние цифры
так же уверенно, как сегодняшние. Поэтому:

- **на маке** дневник знает состояние своего же потока синхронизации и пишет
  «обновлено N мин назад», а при провале — причину последнего круга;
- **на сервере и в боте** знать этого неоткуда, поэтому момент последней
  выгрузки едет вместе с данными и интерфейс предупреждает, если данным больше
  `stats.STALE_AFTER_HOURS` часов. Порог там означает «мак давно не выходил
  на связь», и трёх часов для этого достаточно.

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
