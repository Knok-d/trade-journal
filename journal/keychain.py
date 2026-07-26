"""Доступ к секретам: Keychain на маке, переменные окружения на сервере.

Ключи биржи не лежат в файлах проекта и в репозитории — только здесь.

На macOS источник один: связка `login`. На Linux (сервер Mini App) Keychain
нет, поэтому секреты берутся из окружения, которое systemd подставляет из
файла с правами 0600. Ключи биржи туда не попадают вообще: на сервере нужен
только токен бота, бэкфилл живёт на маке.
"""

import os
import platform
import subprocess

SERVICE = "trade-journal"


class KeychainError(RuntimeError):
    pass


def env_name(account: str) -> str:
    """bybit-key -> TRADE_JOURNAL_BYBIT_KEY"""
    return "TRADE_JOURNAL_" + account.upper().replace("-", "_")


def get(account: str) -> str:
    """Читает секрет. Бросает KeychainError, если его нет."""
    if platform.system() != "Darwin":
        value = os.environ.get(env_name(account))
        if not value:
            raise KeychainError(
                f"Нет переменной {env_name(account)}.\n"
                f"На сервере секреты подставляет systemd из EnvironmentFile."
            )
        return value.strip()

    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        raise KeychainError(
            f"В Keychain нет записи {SERVICE}/{account}.\n"
            f"Добавить:\n"
            f"  security add-generic-password -s {SERVICE} -a {account} -w"
        ) from None
    return out.stdout.strip()


def redact(text: str, *secrets: str) -> str:
    """Вырезает секреты из текста перед логированием или показом ошибки."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, f"<{len(secret)}-char secret>")
    return text
