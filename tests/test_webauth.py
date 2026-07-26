"""Проверка подписи Mini App: атаки должны отбиваться, свой — проходить.

Тесты пишут initData тем же алгоритмом, что и Telegram, поэтому проверяется
реальная криптография, а не заглушка.
"""

import hashlib
import hmac
import json
import sys
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import webauth  # noqa: E402

TOKEN = "1234567890:AAtest_token_value_for_unit_tests_only"
OWNER = 100000001        # выдуманные id: реальные в репозитории не нужны
STRANGER = 100000002


def make_init_data(token=TOKEN, user_id=OWNER, auth_date=None, tamper=None) -> str:
    """Собирает initData так же, как это делает Telegram."""
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAHtest",
        "user": json.dumps({"id": user_id, "first_name": "Дима"}, ensure_ascii=False),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields.update(tamper)
    return urllib.parse.urlencode(fields)


class WebAuthTest(unittest.TestCase):
    def test_valid_owner_passes(self):
        user = webauth.validate(make_init_data(), TOKEN, OWNER)
        self.assertEqual(user["id"], OWNER)

    def test_wrong_token_rejected(self):
        """Подпись чужим токеном — главный сценарий подделки."""
        forged = make_init_data(token="1111:AAforged_token_value_here")
        with self.assertRaises(webauth.AuthError):
            webauth.validate(forged, TOKEN, OWNER)

    def test_tampered_payload_rejected(self):
        """Подменённый user при валидной подписи исходных полей."""
        evil = make_init_data(tamper={
            "user": json.dumps({"id": OWNER, "first_name": "Чужой"})[:-1] + " "})
        with self.assertRaises(webauth.AuthError):
            webauth.validate(evil, TOKEN, OWNER)

    def test_missing_hash_rejected(self):
        without = urllib.parse.urlencode({"auth_date": str(int(time.time()))})
        with self.assertRaises(webauth.AuthError):
            webauth.validate(without, TOKEN, OWNER)

    def test_empty_init_data_rejected(self):
        with self.assertRaises(webauth.AuthError):
            webauth.validate("", TOKEN, OWNER)

    def test_stale_init_data_rejected(self):
        """Перехваченный initData не должен работать вечно."""
        old = make_init_data(auth_date=time.time() - 40 * 3600)
        with self.assertRaises(webauth.AuthError) as ctx:
            webauth.validate(old, TOKEN, OWNER)
        self.assertIn("протух", str(ctx.exception))

    def test_future_auth_date_rejected(self):
        ahead = make_init_data(auth_date=time.time() + 3600)
        with self.assertRaises(webauth.AuthError):
            webauth.validate(ahead, TOKEN, OWNER)

    def test_valid_signature_but_stranger_rejected(self):
        """Чужой Telegram-аккаунт с честной подписью — дневник на одного."""
        other = make_init_data(user_id=STRANGER)
        with self.assertRaises(webauth.AuthError) as ctx:
            webauth.validate(other, TOKEN, OWNER)
        self.assertIn("чужой", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
