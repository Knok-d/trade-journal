"""Проверка подписи Telegram Mini App (initData).

Единственная преграда между публичным URL и историей торговли. Telegram
подписывает данные пользователя ключом, производным от токена бота; сервер
пересчитывает подпись и сверяет. Без этого адрес Mini App — это пароль,
а он утекает первым же скриншотом или логом прокси.

Алгоритм из документации Telegram:
    secret_key = HMAC_SHA256(key="WebAppData", data=bot_token)
    hash       = HMAC_SHA256(key=secret_key, data=data_check_string)
где data_check_string — пары «ключ=значение», кроме hash, отсортированные
по ключу и склеенные через \\n.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

# Подпись не стареет сама по себе, поэтому свежесть проверяется отдельно:
# перехваченный initData иначе работал бы вечно.
MAX_AGE_SECONDS = 24 * 60 * 60


class AuthError(Exception):
    """Подпись не сошлась, протухла или пользователь чужой."""


def validate(init_data: str, bot_token: str, allowed_user_id: int,
             max_age: int = MAX_AGE_SECONDS, now: float | None = None) -> dict:
    """Проверяет initData и возвращает данные пользователя. Иначе AuthError."""
    if not init_data:
        raise AuthError("нет initData")

    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    received = dict(pairs)
    signature = received.pop("hash", None)
    if not signature:
        raise AuthError("в initData нет hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(received.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret_key, check_string.encode(), hashlib.sha256
    ).hexdigest()

    # compare_digest, а не ==: сравнение за постоянное время не даёт
    # подбирать подпись по времени ответа.
    if not hmac.compare_digest(expected, signature):
        raise AuthError("подпись не сошлась")

    try:
        auth_date = int(received.get("auth_date", "0"))
    except ValueError:
        raise AuthError("некорректный auth_date") from None
    age = (now if now is not None else time.time()) - auth_date
    if age > max_age:
        raise AuthError(f"initData протух ({int(age)} с)")
    if age < -300:
        raise AuthError("auth_date из будущего")

    try:
        user = json.loads(received.get("user", "{}"))
    except json.JSONDecodeError:
        raise AuthError("некорректный user") from None

    # Подпись доказывает, что данные от Telegram, но не что это ВЛАДЕЛЕЦ.
    # Дневник — на одного человека, поэтому чужой Telegram-аккаунт отсекается
    # здесь, даже с идеально валидной подписью.
    if user.get("id") != allowed_user_id:
        raise AuthError("чужой пользователь")

    return user
