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
# 1. Пользователь без прав и каталоги
adduser --system --group --home /opt/trade-journal tradejournal
mkdir -p /var/lib/trade-journal /etc/trade-journal
chown tradejournal:tradejournal /var/lib/trade-journal

# 2. Код
git clone https://github.com/Knok-d/trade-journal /opt/trade-journal
chown -R tradejournal:tradejournal /opt/trade-journal

# 3. Секреты. Ключей биржи здесь нет — только токен бота и id владельца
cat > /etc/trade-journal/env <<'EOF'
TRADE_JOURNAL_TELEGRAM_TOKEN=...
TRADE_JOURNAL_TELEGRAM_CHAT_ID=...
EOF
chmod 600 /etc/trade-journal/env
chown root:tradejournal /etc/trade-journal/env

# 4. Сервис
cp deploy/trade-journal-miniapp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trade-journal-miniapp
systemctl status trade-journal-miniapp

# 5. HTTPS
apt install -y caddy
cp deploy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

База лежит в `/var/lib/trade-journal/journal.db`; путь задаётся переменной
`HOME` пользователя сервиса — проверить, что процесс видит именно её.

## Первое заполнение данными

```sh
# на маке: выгрузка вместе с уже накопленными разборами
python3 -m journal.cli export /tmp/transfer.db --with-journal
scp /tmp/transfer.db root@СЕРВЕР:/tmp/

# на сервере
sudo -u tradejournal python3 -m journal.cli import /tmp/transfer.db
rm /tmp/transfer.db
```

Дальнейшие синхронизации — без `--with-journal`: заметки уже живут на сервере,
и заливка их не трогает.

## Кнопка в боте

У @BotFather: `/mybots` → бот → Bot Settings → Menu Button → задать URL домена.

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
